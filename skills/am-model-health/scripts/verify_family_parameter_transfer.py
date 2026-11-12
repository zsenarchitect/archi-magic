"""Verify independent rollback and Keep or Discard records for a private family transfer."""

import argparse
import json
import re
import sys

from family_library_discovery import manifest_digest
from family_parameter_transfer_effects import verify_element_effects, verify_family_effects
from family_parameter_transfer_preview import preview, reviewed_snapshot
from family_placement_preview import load_json
from probe_family_parameter_transfer import TRANSACTION_NAME
from revit_probe_record import require_no_changes, require_run
from verify_family_placement_probe import bridge_effects, unique_ids


def read_audit(run):
    result = run["result"]
    token = result.get("audit_token")
    if (result.get("status") != "ok" or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token)):
        raise ValueError("Use a complete independent family audit.")
    return json.loads(result["snapshot_json"]), token


def measured(plan, baseline, result, status):
    transaction = result.get("transaction")
    if (result.get("status") != status or result.get("preview_sha256") != plan["preview_sha256"]
            or result.get("audit_sha256") != manifest_digest(baseline)
            or not isinstance(transaction, str)
            or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [0-9a-f]{32}", transaction)):
        raise ValueError("The trial or replay does not bind the reviewed family baseline.")

    added = result["added_ids"]
    planned = {name: row["expected_values"] for name, row in plan["operations"].items() if row["action"] == "add"}
    expected = {type_name: {name: values[type_name] for name, values in planned.items()}
                for type_name in baseline["state"]["types"]}
    known = baseline["inventory"].keys() | baseline["state"]["definitions"].keys()
    if (not planned or added.keys() != planned.keys() or set(added.values()) & known
            or len(unique_ids(list(added.values()))) != len(planned) or result["values"] != expected):
        raise ValueError("The measured additions or type values differ from the preview.")
    events = result["effects"]
    if len(events) != 1 or events[0].keys() != {"added", "deleted", "modified"}:
        raise ValueError("Use one complete family event record.")
    event = {key: unique_ids(rows) for key, rows in events[0].items()}
    if (not (event["added"] or event["modified"]) or event["deleted"]
            or set(event["added"]) - set(added.values()) or set(event["modified"]) - {baseline["owner"]}):
        raise ValueError("The trial reports effects outside the reviewed additions.")
    return event


def effect_roles(result):
    names = {identifier: name for name, identifier in result["added_ids"].items()}
    event = result["effects"][0]
    return {"added": sorted(names[identifier] for identifier in event["added"]),
            "modified": sorted(event["modified"]), "deleted": sorted(event["deleted"])}


def verify(transfer, before_record, trial_record, after_record):
    plan = preview(transfer["source_read"], transfer["target_read"], transfer["request"])
    before = require_no_changes(before_record, "query")
    trial = require_run(trial_record, "dry_run", ("rolled_back",))
    after = require_no_changes(after_record, "query")
    run_ids = [record["run_id"] for record in (before, trial, after)]
    baseline, token = read_audit(before)
    restored, restored_token = read_audit(after)
    target = reviewed_snapshot(transfer["target_read"], "target", plan["target_sha256"])
    result = trial["result"]
    if (len(set(run_ids)) != 3 or token == restored_token or baseline != restored or baseline["kept"] is not False
            or baseline["state"] != target["state"] or baseline["owner"] not in baseline["inventory"]
            or baseline["copy_sha256"] != manifest_digest(transfer["working_copy"])
            or baseline["preview_sha256"] != plan["preview_sha256"] or transfer["preview_sha256"] != plan["preview_sha256"]
            or before["result"].get("trial_transaction") is not None
            or after["result"].get("trial_transaction") != result.get("transaction")
            or result.get("rollback_verified") is not True):
        raise ValueError("The independent audits do not prove rollback of the reviewed family.")
    effects = measured(plan, baseline, result, "trial_rolled_back")
    bridge_effects(trial, effects, result["transaction"])
    return plan, baseline, result, run_ids


def verify_decision(transfer, before, trial, after, modify_record, fresh_record):
    plan, baseline, rolled_back, run_ids = verify(transfer, before, trial, after)
    decision = modify_record.get("response", modify_record).get("decision")
    if decision not in {"committed", "discarded"}:
        raise ValueError("Use a saved Revit Keep or Discard decision.")
    kept = decision == "committed"
    modify = require_run(modify_record, "modify", (decision,), approval="user", status="ok" if kept else "rejected")
    fresh = require_no_changes(fresh_record, "query")
    run_ids += [modify["run_id"], fresh["run_id"]]
    current, fresh_token = read_audit(fresh)
    previous_tokens = {read_audit(require_no_changes(record, "query"))[1] for record in (before, after)}
    result = modify["result"]
    effects = measured(plan, baseline, result, "awaiting_bridge_decision")
    if (len(set(run_ids)) != 5 or fresh_token in previous_tokens
            or result["transaction"] == rolled_back["transaction"]
            or fresh["result"].get("trial_transaction") != result["transaction"]
            or effect_roles(result) != effect_roles(rolled_back) or current["kept"] is not kept):
        raise ValueError("The records do not prove the reviewed native decision.")
    bridge_effects(modify, effects, result["transaction"], decision=decision)
    if kept:
        for key in ("preview_sha256", "copy_sha256", "owner"):
            if current[key] != baseline[key]:
                raise ValueError("The kept family identity differs from the reviewed copy.")
        verify_family_effects(plan, baseline["state"], current["state"], result["added_ids"])
        verify_element_effects(baseline["inventory"], current["inventory"], [effects], baseline["owner"], result["added_ids"])
    elif current != baseline:
        raise ValueError("The family baseline was not restored after Discard.")
    receipt = {"status": "family_transfer_kept" if kept else "family_transfer_discarded",
               "preview_sha256": plan["preview_sha256"], "copy_sha256": baseline["copy_sha256"],
               "fresh_sha256": manifest_digest(current), "run_ids": run_ids,
               "transaction": result["transaction"], "added_ids": result["added_ids"] if kept else {}}
    return dict(receipt, decision_sha256=manifest_digest(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("transfer", "before", "trial", "after"):
        parser.add_argument(name)
    parser.add_argument("--modify")
    parser.add_argument("--fresh")
    args = parser.parse_args()
    try:
        inputs = [load_json(getattr(args, name)) for name in ("transfer", "before", "trial", "after")]
        if bool(args.modify) != bool(args.fresh):
            raise ValueError("Supply both the native decision and its fresh audit.")
        if args.modify:
            result = verify_decision(*inputs, load_json(args.modify), load_json(args.fresh))
        else:
            plan, baseline, trial, run_ids = verify(*inputs)
            result = {"status": "family_transfer_rollback_verified", "preview_sha256": plan["preview_sha256"],
                      "audit_sha256": manifest_digest(baseline), "values": trial["values"], "run_ids": run_ids}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Family transfer verification blocked. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
