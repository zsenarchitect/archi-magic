"""Verify rollback or an independently audited Revit project reload decision."""

import argparse
import json
import re
import sys

from family_library_discovery import manifest_digest
from family_parameter_reload_preview import baseline_record
from family_placement_preview import load_json
from probe_family_parameter_reload import TRANSACTION_NAME, reviewed_reload, target_scopes
from revit_probe_record import require_no_changes, require_run
from verify_family_placement_probe import bridge_effects, unique_ids


def sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def token(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def measured(preview, baseline, before_token, result, status):
    name = result.get("transaction")
    if (result.get("status") != status or result.get("preview_sha256") != preview["preview_sha256"]
            or result.get("baseline_sha256") != baseline["baseline_sha256"]
            or result.get("baseline_audit_token") != before_token
            or not isinstance(name, str) or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [0-9a-f]{32}", name)):
        raise ValueError("The reload record does not bind the reviewed project baseline.")
    families = result["families"]
    if len(families) != len(preview["rows"]):
        raise ValueError("The reload did not measure every reviewed family.")
    for item, row in zip(families, preview["rows"]):
        if (item["loaded_family"] != row["loaded_family"] or item["state_sha256"] != row["receipt"]["state_sha256"]
                or not sha256(item["project_values_sha256"])):
            raise ValueError("The reload reported a different family or incomplete project values.")
    effects = result["effects"]
    if effects.keys() != {"added", "deleted", "modified"}:
        raise ValueError("The reload effect record is incomplete.")
    effects = {key: unique_ids(values) for key, values in effects.items()}
    scopes = target_scopes(baseline)
    modified = set(effects["modified"])
    if (effects["added"] or effects["deleted"] or modified - set().union(*scopes)
            or any(not modified & scope for scope in scopes)
            or result["inventory"]["element_count"] != baseline["inventory"]["element_count"]
            or not sha256(result["inventory"]["sha256"])):
        raise ValueError("The reload effects exceed the reviewed project scopes.")
    return effects


def verify(reload, trial_record, after_record):
    baseline, before_token = reviewed_reload(reload)
    preview = reload["preview"]
    before = require_no_changes(reload["baseline"], "query")
    trial = require_run(trial_record, "dry_run", ("rolled_back",))
    after = require_no_changes(after_record, "query")
    result = trial["result"]
    after_token = after["result"].get("audit_token")
    run_ids = [record["run_id"] for record in (before, trial, after)]
    if (len(set(run_ids)) != 3 or not token(after_token) or after_token == before_token
            or baseline_record(after) != baseline or after["result"].get("trial_transaction") != result.get("transaction")
            or result.get("rollback_verified") is not True):
        raise ValueError("The independent project audit does not prove exact rollback.")
    effects = measured(preview, baseline, before_token, result, "trial_rolled_back")
    bridge_effects(trial, effects, result["transaction"])
    return baseline, before_token, result, effects, run_ids


def verify_decision(reload, trial, after, modify_record, fresh_record):
    baseline, before_token, rolled_back, old_effects, run_ids = verify(reload, trial, after)
    decision = modify_record.get("response", modify_record).get("decision")
    if decision not in {"committed", "discarded"}:
        raise ValueError("Use the saved Revit Keep or Discard decision.")
    kept = decision == "committed"
    modify = require_run(modify_record, "modify", (decision,), approval="user", status="ok" if kept else "rejected")
    fresh = require_no_changes(fresh_record, "query")
    result = modify["result"]
    preview = reload["preview"]
    effects = measured(preview, baseline, before_token, result, "awaiting_bridge_decision")
    bridge_effects(modify, effects, result["transaction"], decision=decision)
    run_ids += [modify["run_id"], fresh["run_id"]]
    audit = fresh["result"]
    after_token = require_no_changes(after, "query")["result"]["audit_token"]
    if (len(set(run_ids)) != 5 or audit.get("status") != "ok" or not token(audit.get("audit_token"))
            or audit["audit_token"] in {before_token, after_token}
            or result["transaction"] == rolled_back["transaction"] or audit.get("trial_transaction") != result["transaction"]
            or effects != old_effects or result["families"] != rolled_back["families"]):
        raise ValueError("The records do not prove the reviewed native reload decision.")
    if kept:
        if (audit.get("preview_sha256") != preview["preview_sha256"] or audit.get("project") != baseline["project"]
                or audit.get("inventory") != result["inventory"] or audit.get("families") != result["families"]):
            raise ValueError("The kept project differs from the exact reviewed reload.")
    elif baseline_record(fresh) != baseline:
        raise ValueError("The project baseline was not restored after Discard.")
    receipt = {"status": "project_reload_kept" if kept else "project_reload_discarded",
               "preview_sha256": preview["preview_sha256"], "project": baseline["project"],
               "transaction": result["transaction"], "run_ids": run_ids, "fresh_sha256": manifest_digest(audit)}
    return dict(receipt, decision_sha256=manifest_digest(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reload", "trial", "after"):
        parser.add_argument(name)
    parser.add_argument("--modify")
    parser.add_argument("--fresh")
    args = parser.parse_args()
    try:
        records = [load_json(getattr(args, name)) for name in ("reload", "trial", "after")]
        if bool(args.modify) != bool(args.fresh):
            raise ValueError("Supply both the native decision and the independent project audit.")
        if args.modify:
            result = verify_decision(*records, load_json(args.modify), load_json(args.fresh))
        else:
            baseline, _, trial, effects, run_ids = verify(*records)
            result = {"status": "project_reload_rollback_verified", "preview_sha256": trial["preview_sha256"],
                      "baseline_sha256": baseline["baseline_sha256"], "effects": effects, "run_ids": run_ids}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Project reload verification blocked. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
