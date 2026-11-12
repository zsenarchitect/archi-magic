"""Verify the family derivation trial against independent saved Revit queries."""

import argparse
import json
import re
import sys

from batch_family_derivation_preview import build_preview, working_read
from batch_family_type_preview import validate_record
from family_library_discovery import manifest_digest
from family_placement_preview import load_json
from probe_batch_family_derivation import TRANSACTION_NAME, compare_state
from revit_probe_record import require_no_changes, require_run
from verify_family_placement_probe import bridge_effects, unique_ids


def audit(result):
    token = result.get("audit_token")
    if (result.get("status") != "ok" or not isinstance(result.get("snapshot_json"), str)
            or not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token)):
        raise ValueError("Use the full saved working-family query with its audit token.")
    return working_read(result), token


def measured_types(plan, baseline, result):
    types = result["types"]
    if not isinstance(types, dict) or set(types) & set(baseline["state"]["types"]):
        raise ValueError("The saved trial includes an existing family type.")
    simulated = dict(baseline["state"], types={**baseline["state"]["types"], **types})
    return compare_state(plan, baseline["state"], simulated)


def exact_effects(result, owner):
    events = result["effects"]
    if not isinstance(events, list) or len(events) > 1:
        raise ValueError("Use one exact family derivation effect record.")
    effects = {"added": [], "deleted": [], "modified": []}
    if events:
        if set(events[0]) != set(effects):
            raise ValueError("The family effect record is incomplete.")
        effects = {key: unique_ids(values) for key, values in events[0].items()}
    if effects["added"] or effects["deleted"] or set(effects["modified"]) - {owner}:
        raise ValueError("The trial reports effects outside the reviewed family owner.")
    return effects


def verify(plan, preview, before_record, trial_record, after_record):
    before = require_no_changes(before_record, "query")
    trial = require_run(trial_record, "dry_run", ("no_changes", "rolled_back"))
    after = require_no_changes(after_record, "query")
    run_ids = [record["run_id"] for record in (before, trial, after)]
    if len(set(run_ids)) != 3:
        raise ValueError("Save three independent Revit run records.")
    baseline, token = audit(before["result"])
    fresh, fresh_token = audit(after["result"])
    result = trial["result"]
    transaction = result.get("transaction")
    if (build_preview(plan, baseline) != preview or baseline != fresh or token == fresh_token
            or before["result"].get("trial_transaction") is not None
            or after["result"].get("trial_transaction") != transaction
            or result.get("status") != "trial_rolled_back" or result.get("rollback_verified") is not True
            or result.get("preview_sha256") != manifest_digest(preview)
            or result.get("working_read_sha256") != baseline["working_read_sha256"]
            or result.get("baseline_audit_token") != token
            or not isinstance(transaction, str) or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [0-9a-f]{32}", transaction)):
        raise ValueError("The saved trial and fresh family read do not prove this exact reviewed rollback.")
    types = measured_types(plan, baseline, result)
    effects = exact_effects(result, baseline["owner"]["id"])
    bridge_effects(trial, effects, transaction)
    return {"status": "batch_family_derivation_rollback_verified", "derivation_sha256": plan["derivation_sha256"],
            "preview_sha256": result["preview_sha256"], "working_read_sha256": baseline["working_read_sha256"],
            "baseline_audit_token": token, "types": types, "effects": result["effects"], "run_ids": run_ids}


def verify_decision(plan, preview, before, trial, after, modify, fresh):
    proof = verify(plan, preview, before, trial, after)
    record = modify.get("response", modify)
    decision = record.get("decision")
    if decision not in {"committed", "discarded"}:
        raise ValueError("Use the full saved user Keep or Discard record.")
    kept = decision == "committed"
    modify = require_run(modify, "modify", (decision,), approval="user", status="ok" if kept else "rejected")
    fresh = require_no_changes(fresh, "query")
    run_ids = proof["run_ids"] + [modify["run_id"], fresh["run_id"]]
    if len(set(run_ids)) != 5:
        raise ValueError("Use five independent bridge run records.")
    initial = before.get("response", before)["result"]
    baseline, initial_token = audit(initial)
    restored_record = after.get("response", after)["result"]
    snapshot, token = audit(fresh["result"])
    validate_record(snapshot, "batch-family-working-read", "working_read_sha256")
    result = modify["result"]
    transaction = result.get("transaction")
    trial_result = trial.get("response", trial)["result"]
    if (result.get("status") != "awaiting_bridge_decision" or result.get("preview_sha256") != proof["preview_sha256"]
            or result.get("working_read_sha256") != proof["working_read_sha256"]
            or result.get("baseline_audit_token") != initial_token
            or not isinstance(transaction, str) or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [0-9a-f]{32}", transaction)
            or transaction == trial_result["transaction"] or fresh["result"].get("trial_transaction") != transaction
            or token in {initial_token, restored_record["audit_token"]}):
        raise ValueError("The modify and fresh records do not bind the reviewed family decision.")
    effects = exact_effects(result, baseline["owner"]["id"])
    if (not effects["modified"] or result["effects"] != proof["effects"]
            or measured_types(plan, baseline, result) != proof["types"]):
        raise ValueError("The final effect or derived values differ from the verified trial.")
    bridge_effects(modify, effects, transaction, decision=decision)
    if kept:
        expected = dict(baseline["state"], types={**baseline["state"]["types"], **proof["types"]})
        for field in ("derivation_sha256", "family", "category_id", "owner", "revit_build", "preserved_inventory"):
            if snapshot[field] != baseline[field]:
                raise ValueError("The retained family identity or unedited inventory changed.")
        if (snapshot["kept"] is not True or snapshot["state"] != expected
                or snapshot["inventory"]["element_count"] != baseline["inventory"]["element_count"]):
            raise ValueError("The retained family state differs from the reviewed derivation.")
    elif snapshot != baseline:
        raise ValueError("Discard did not restore the original family evidence.")
    evidence = {"kind": "batch-family-derivation-decision", "version": 1,
                "status": "kept_verified" if kept else "discard_verified",
                "derivation_sha256": plan["derivation_sha256"], "preview_sha256": proof["preview_sha256"],
                "revit_build": baseline["revit_build"], "family": baseline["family"],
                "category_id": baseline["category_id"], "state": snapshot["state"],
                "fresh_run_id": fresh["run_id"], "modify_run_id": modify["run_id"]}
    return dict(evidence, decision_sha256=manifest_digest(evidence))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("derivation", "preview", "before", "trial", "after")
    for name in names:
        parser.add_argument(name)
    parser.add_argument("--modify")
    parser.add_argument("--fresh")
    args = parser.parse_args()
    if bool(args.modify) != bool(args.fresh):
        parser.error("Supply both --modify and --fresh to verify a final decision.")
    try:
        records = [load_json(getattr(args, name)) for name in names]
        proof = (verify_decision(*records, load_json(args.modify), load_json(args.fresh))
                 if args.modify else verify(*records))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(proof, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
