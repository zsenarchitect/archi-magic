"""Verify the saved user decision and the fresh state of a reviewed family placement."""

import argparse
import json
import re
import sys

from family_library_discovery import manifest_digest
from family_placement_preview import finite, load_json
from probe_family_placement import POSITION_TOLERANCE_FT, ROTATION_TOLERANCE_DEG, TRANSACTION_NAME
from revit_probe_record import require_no_changes, require_run
from verify_family_placement_probe import bridge_effects, read_audit, unique_ids, verify as verify_trial, verify_pose


def kept_state(review, baseline, snapshot, result, fresh):
    measured = result["placement"]
    pose = fresh["pose"]
    point = pose["xyz_ft"]
    angle = (finite(pose["rotation_deg"]) - finite(measured["rotation_deg"]) + 180) % 360 - 180
    symbol_uid = review["symbol"]["uid"] if review["action"] == "reuse" else result["loaded_symbol"]["uid"]
    if (not isinstance(point, list) or len(point) != 3
            or any(abs(finite(actual) - expected) > POSITION_TOLERANCE_FT
                   for actual, expected in zip(point, measured["xyz_ft"]))
            or abs(angle) > ROTATION_TOLERANCE_DEG
            or any(pose[key] != measured[key] for key in ("id", "uid", "level_id", "symbol_id"))
            or pose["level_uid"] != review["level"]["uid"] or pose["symbol_uid"] != symbol_uid):
        raise ValueError("The retained instance identity, type, level, or pose differs from the handoff.")
    expected = dict(baseline)
    if review["action"] == "reuse":
        expected["exact_symbols"] = [dict(review["symbol"], active=True)]
    else:
        loaded = result["loaded_symbol"]
        selection = review["selection"]
        actual = snapshot["exact_symbols"]
        if len(actual) != 1:
            raise ValueError("The kept project does not expose one exact loaded symbol.")
        values = actual[0]["parameters"]
        if any(values.get(field["id"]) != selection["type_values"][field["id"]]
               for field in selection["parameters"] if field["storage"] in {"String", "Double", "Integer"}):
            raise ValueError("The retained catalog or family type values differ from the staged type.")
        expected["exact_symbols"] = [dict(loaded, active=True, parameters=values, placement_type="OneLevelBased")]
        expected["same_name_families"] = [{
            "id": loaded["family_id"], "uid": loaded["family_uid"], "name": selection["family"],
            "category_id": selection["category"]["id"], "placement_type": "OneLevelBased",
        }]
    if expected != snapshot:
        raise ValueError("The kept project type or level differs from the reviewed effect.")


def verify(preview, before, trial, after, modify, fresh):
    proof = verify_trial(preview, before, trial, after)
    record = modify.get("response", modify)
    decision = record.get("decision")
    if decision not in {"committed", "discarded"}:
        raise ValueError("Use the saved bridge record for the user's Keep or Discard decision.")
    kept = decision == "committed"
    modify = require_run(modify, "modify", (decision,), approval="user", status="ok" if kept else "rejected")
    fresh = require_no_changes(fresh, "query")
    if len(set(proof["run_ids"] + [modify["run_id"], fresh["run_id"]])) != 5:
        raise ValueError("Save five independent Revit run records.")
    result = modify["result"]
    transaction = result.get("transaction")
    if (result.get("status") != "awaiting_bridge_decision" or result.get("preview_sha256") != manifest_digest(preview)
            or not isinstance(transaction, str) or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [a-f0-9]{32}", transaction)):
        raise ValueError("The saved modify record is not the reviewed handoff.")
    trial_record = trial.get("response", trial)
    if transaction == trial_record["result"]["transaction"]:
        raise ValueError("The final decision must use its own transaction.")
    review = preview["review"]
    added = verify_pose(review, result)
    effects = {"added": added, "deleted": [], "modified": proof["effects"]["modified"]}
    if (set(result["effects"]) != set(effects)
            or any(unique_ids(result["effects"][key]) != value for key, value in effects.items())):
        raise ValueError("The final provisional effects differ from the reviewed scope.")
    bridge_effects(modify, effects, transaction, decision=decision)

    before = before.get("response", before)
    baseline, baseline_inventory, _ = read_audit(before)
    snapshot, inventory, absent = read_audit(fresh)
    fresh_result = fresh["result"]
    rollback_query = after.get("response", after)
    preserved = before["result"]["preserved_inventory"]
    if (not isinstance(preserved.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", preserved["sha256"])
            or type(preserved.get("element_count")) is not int
            or preserved["element_count"] != baseline_inventory["element_count"] - len(effects["modified"])):
        raise ValueError("The baseline does not identify the inventory outside the approved write scope.")
    expected_present = unique_ids(effects["modified"] + (added if kept else []))
    if (fresh_result.get("trial_transaction") != transaction
            or fresh_result["audit_token"] in {before["result"]["audit_token"], rollback_query["result"]["audit_token"]}
            or unique_ids(fresh_result["present_ids"]) != expected_present
            or absent != ([] if kept else added)
            or fresh_result["preserved_inventory"] != preserved):
        raise ValueError("The fresh read does not prove the exact decision and preserved project scope.")
    if kept:
        if inventory["element_count"] != baseline_inventory["element_count"] + len(added):
            raise ValueError("The retained project element count differs from the reviewed additions.")
        kept_state(review, baseline, snapshot, result, fresh_result)
    elif baseline != snapshot or baseline_inventory != inventory or fresh_result["pose"] is not None:
        raise ValueError("Discard did not restore the original project evidence.")
    return {"status": "family_placement_keep_verified" if kept else "family_placement_discard_verified",
            "project": baseline["project"], "revit_build": baseline["revit_build"],
            "preview_sha256": result["preview_sha256"], "modify_run_id": modify["run_id"],
            "fresh_run_id": fresh["run_id"], "effects": effects, "placement": fresh_result["pose"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("preview", "before", "trial", "after", "modify", "fresh")
    for name in names:
        parser.add_argument(name)
    args = parser.parse_args()
    try:
        result = verify(*(load_json(getattr(args, name)) for name in names))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
