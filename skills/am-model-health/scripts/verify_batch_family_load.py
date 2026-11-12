"""Verify a batch project load from independent saved Revit runs."""

import argparse
import json
import re
import sys

from batch_family_load_preview import build_load_preview
from family_library_discovery import manifest_digest
from family_placement_preview import load_json
from probe_batch_family_load import TRANSACTION_NAME, check_loaded_project
from revit_probe_record import require_no_changes, require_run
from verify_family_placement_probe import bridge_effects, unique_ids


def audit(record):
    result = record["result"]
    token = result.get("audit_token")
    if (result.get("status") != "ok" or not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token)):
        raise ValueError("Use a full independent batch project audit.")
    snapshot = json.loads(result["snapshot_json"])
    for field in ("inventory", "preserved_inventory"):
        inventory = result[field]
        if (type(inventory.get("element_count")) is not int or inventory["element_count"] < 0
                or not re.fullmatch("[0-9a-f]{64}", inventory["sha256"])):
            raise ValueError("The project inventory record is invalid.")
    return snapshot, result


def approved_effects(preview, result):
    rows = {}
    families = result["families"]
    added = []
    sources = set()
    uids = []
    for group in preview["review"]["exports"]:
        source = group["receipt"]["source_path"]
        sources.add(source)
        family = families[source]
        added.append(family["id"])
        uids.append(family["uid"])
        for row in group["rows"]:
            key = str(row["line"])
            actual = result["rows"][key]
            if (set(actual) != {"family_id", "family_uid", "symbol_id", "symbol_uid"}
                    or actual["family_id"] != family["id"] or actual["family_uid"] != family["uid"]):
                raise ValueError("The reported types do not belong to their exact loaded family.")
            rows[int(key)] = actual
            added.append(actual["symbol_id"])
            uids.append(actual["symbol_uid"])
    effects = {"added": unique_ids(added), "modified": [], "deleted": []}
    if (not rows or set(families) != sources or set(result["rows"]) != {str(key) for key in rows}
            or not all(isinstance(uid, str) and uid.strip() for uid in uids) or len(set(uids)) != len(uids)
            or result["effects"] != effects):
        raise ValueError("The batch effects do not match the approved family and type rows.")
    return rows, effects


def check_result(preview, baseline, original, result):
    transaction = result.get("transaction")
    if (not isinstance(transaction, str)
            or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [0-9a-f]{32}", transaction)
            or result.get("preview_sha256") != manifest_digest(preview)
            or result.get("baseline_sha256") != manifest_digest(baseline)
            or result.get("baseline_inventory") != original["inventory"]
            or result.get("baseline_audit_token") != original["audit_token"]):
        raise ValueError("The saved trial does not bind the exact preview and original project.")
    return transaction


def verify(lookup, staging, preview, before, trial, after):
    before = require_no_changes(before, "query")
    trial = require_run(trial, "dry_run", ("no_changes", "rolled_back"))
    after = require_no_changes(after, "query")
    run_ids = [record["run_id"] for record in (before, trial, after)]
    if len(set(run_ids)) != 3:
        raise ValueError("Use three independent bridge run records.")
    baseline, original = audit(before)
    restored, fresh = audit(after)
    exports = [group["receipt"]["workspace"] for group in preview["review"]["exports"]]
    if build_load_preview(lookup, baseline, staging, exports) != preview:
        raise ValueError("The saved preview differs from the exact project and verified exports.")
    result = trial["result"]
    transaction = check_result(preview, baseline, original, result)
    _, effects = approved_effects(preview, result)
    if (result["status"] != "trial_rolled_back" or result.get("rollback_verified") is not True
            or original["transaction"] is not None or original["present_ids"] or original["absent_ids"]
            or fresh["audit_token"] == original["audit_token"] or fresh["transaction"] != transaction
            or fresh["present_ids"] or fresh["absent_ids"] != effects["added"]
            or baseline != restored or original["inventory"] != fresh["inventory"]
            or fresh["preserved_inventory"] != original["inventory"]):
        raise ValueError("The independent read does not prove exact project restoration.")
    bridge_effects(trial, effects, transaction)
    return {"status": "batch_family_load_rollback_verified", "run_ids": run_ids,
            "baseline_sha256": result["baseline_sha256"], "baseline_inventory": original["inventory"],
            "baseline_audit_token": original["audit_token"], "effects": effects}


def verify_decision(lookup, staging, preview, before, trial, after, modify, fresh):
    proof = verify(lookup, staging, preview, before, trial, after)
    record = modify.get("response", modify)
    decision = record.get("decision")
    if decision not in {"committed", "discarded"}:
        raise ValueError("Use the saved bridge record for the user's Keep or Discard decision.")
    kept = decision == "committed"
    modify = require_run(modify, "modify", (decision,), approval="user", status="ok" if kept else "rejected")
    fresh = require_no_changes(fresh, "query")
    if len(set(proof["run_ids"] + [modify["run_id"], fresh["run_id"]])) != 5:
        raise ValueError("Use five independent bridge run records.")
    baseline, original = audit(before.get("response", before))
    _, restored = audit(after.get("response", after))
    observed, current = audit(fresh)
    result = modify["result"]
    transaction = check_result(preview, baseline, original, result)
    rows, effects = approved_effects(preview, result)
    if (result["status"] != "awaiting_bridge_decision"
            or transaction == trial.get("response", trial)["result"]["transaction"]
            or current["transaction"] != transaction
            or current["audit_token"] in {original["audit_token"], restored["audit_token"]}
            or current["preserved_inventory"] != original["inventory"]
            or current["present_ids"] != (effects["added"] if kept else [])
            or current["absent_ids"] != ([] if kept else effects["added"])):
        raise ValueError("The fresh project audit does not prove the recorded decision.")
    bridge_effects(modify, effects, transaction, decision=decision)
    if kept:
        if current["inventory"]["element_count"] != original["inventory"]["element_count"] + len(effects["added"]):
            raise ValueError("The kept inventory does not match the exact additions.")
        check_loaded_project(baseline, observed, rows)
    elif observed != baseline or current["inventory"] != original["inventory"]:
        raise ValueError("Discard did not restore the original project.")
    return {"status": "batch_family_load_keep_verified" if kept else "batch_family_load_discard_verified",
            "project": baseline["project"], "modify_run_id": modify["run_id"], "fresh_run_id": fresh["run_id"],
            "effects": effects}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("lookup", "staging", "preview", "before", "trial", "after")
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
