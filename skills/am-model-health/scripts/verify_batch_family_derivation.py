"""Verify the family derivation trial against independent saved Revit queries."""

import argparse
import json
import re
import sys

from batch_family_derivation_preview import build_preview, working_read
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("derivation", "preview", "before", "trial", "after")
    for name in names:
        parser.add_argument(name)
    args = parser.parse_args()
    try:
        proof = verify(*(load_json(getattr(args, name)) for name in names))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(proof, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
