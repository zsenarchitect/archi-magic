"""Verify a family placement rollback from three full saved Revit run records."""

import argparse
import json
import re
import sys

from family_library_discovery import manifest_digest
from family_placement_preview import build_preview, finite, load_json, project_snapshot
from probe_family_placement import POSITION_TOLERANCE_FT, ROTATION_TOLERANCE_DEG, TRANSACTION_NAME
from read_family_placement import MAX_ELEMENTS
from revit_probe_record import require_no_changes, require_run


def identifier(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) >= 2**63:
        raise ValueError("Use canonical positive Revit element IDs.")
    return value


def unique_ids(items):
    values = [identifier(item) for item in items]
    if len(set(values)) != len(values):
        raise ValueError("The trial contains duplicate effect IDs.")
    return sorted(values, key=int)


def read_audit(record):
    result = record["result"]
    snapshot = project_snapshot(result)
    inventory = result["inventory"]
    if (result.get("status") != "ok" or not isinstance(result.get("snapshot_json"), str)
            or not isinstance(inventory.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", inventory["sha256"])
            or type(inventory.get("element_count")) is not int or not 1 <= inventory["element_count"] <= MAX_ELEMENTS
            or not isinstance(result.get("absent_ids"), list)
            or not isinstance(result.get("audit_token"), str) or not re.fullmatch("[0-9a-f]{32}", result["audit_token"])):
        raise ValueError("Use a complete independent project inventory read.")
    return snapshot, inventory, unique_ids(result["absent_ids"])


def verify_pose(review, result):
    pose = result["placement"]
    point = pose["xyz_ft"]
    angle = finite(pose["rotation_deg"])
    if (not isinstance(point, list) or len(point) != 3
            or any(abs(finite(actual) - target) > POSITION_TOLERANCE_FT
                   for actual, target in zip(point, review["absolute_xyz_ft"]))
            or abs((angle - review["rotation_deg"] + 180) % 360 - 180) > ROTATION_TOLERANCE_DEG
            or identifier(pose["level_id"]) != review["level"]["id"]
            or not isinstance(pose.get("uid"), str) or not pose["uid"].strip()):
        raise ValueError("The saved trial pose does not match the reviewed location, rotation, and level.")
    if review["action"] == "reuse":
        if result["loaded_symbol"] is not None or identifier(pose["symbol_id"]) != review["symbol"]["id"]:
            raise ValueError("The trial did not reuse the exact project type.")
        return [identifier(pose["id"])]
    loaded = result["loaded_symbol"]
    if (not isinstance(loaded, dict) or identifier(pose["symbol_id"]) != identifier(loaded["id"])
            or not all(isinstance(loaded.get(key), str) and loaded[key].strip() for key in ("family_uid", "uid"))):
        raise ValueError("The trial did not report one exact new family and symbol.")
    return unique_ids([pose["id"], loaded["id"], loaded["family_id"]])


def bridge_effects(record, expected, transaction):
    changes = record["changes"]
    reported = {}
    for kind in ("added", "deleted", "modified"):
        count = changes.get(kind + "_count")
        rows = changes.get(kind, [])
        if (type(count) is not int or not isinstance(rows, list) or count != len(rows)
                or changes.get(kind + "_ids")):
            raise ValueError("Save the full Revit run record with all effect IDs.")
        reported[kind] = unique_ids([row["id"] for row in rows])
    if changes.get("other_documents"):
        raise ValueError("The bridge reports effects in another document.")
    categories = changes.get("by_category")
    total = sum(len(rows) for rows in reported.values())
    if (not isinstance(categories, dict) or any(type(count) is not int or count <= 0 for count in categories.values())
            or sum(categories.values()) != total):
        raise ValueError("The bridge effect totals disagree with its element lists.")
    transactions = changes.get("transactions", [])
    if total:
        if record["decision"] != "rolled_back" or reported != expected or transactions != [transaction]:
            raise ValueError("The bridge reports effects outside the reviewed provisional transaction.")
    elif transactions and (record["decision"] != "rolled_back" or transactions != [transaction]):
        raise ValueError("The bridge reports an unrelated transaction.")


def verify(preview, before_record, trial_record, after_record):
    before = require_no_changes(before_record, "query")
    trial = require_run(trial_record, "dry_run", ("no_changes", "rolled_back"))
    after = require_no_changes(after_record, "query")
    run_ids = [record["run_id"] for record in (before, trial, after)]
    if len(set(run_ids)) != 3:
        raise ValueError("Save three independent Revit run records.")
    baseline, inventory, absent = read_audit(before)
    fresh, fresh_inventory, fresh_absent = read_audit(after)
    review = preview["review"]
    selection = review["selection"]
    expected = build_preview(selection, baseline, review["absolute_xyz_ft"], review["rotation_deg"])
    if (preview["review_sha256"] != manifest_digest(review) or expected["review"] != review
            or baseline != fresh or inventory != fresh_inventory or absent or baseline["revit_year"] != "2026"
            or before["result"].get("trial_transaction") is not None
            or before["result"]["audit_token"] == after["result"]["audit_token"]
            or review["action"] not in {"reuse", "load"} or review["placement_type"] != "OneLevelBased"):
        raise ValueError("The reviewed placement and restored project evidence disagree.")
    result = trial["result"]
    transaction = result.get("transaction")
    if (result.get("status") != "trial_rolled_back" or result.get("rollback_verified") is not True
            or result.get("load_conflicts") != [] or result.get("preview_sha256") != manifest_digest(preview)
            or result.get("baseline_sha256") != manifest_digest(baseline)
            or result.get("baseline_inventory") != inventory
            or result.get("baseline_audit_token") != before["result"]["audit_token"]
            or after["result"].get("trial_transaction") != transaction
            or not isinstance(transaction, str) or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [a-f0-9]{32}", transaction)):
        raise ValueError("The native trial does not prove this exact reviewed rollback.")
    added = verify_pose(review, result)
    effects = {"added": added, "deleted": [], "modified": unique_ids(review["ownership_ids"])}
    if (fresh_absent != added or set(result["effects"]) != set(effects)
            or any(unique_ids(result["effects"][key]) != value for key, value in effects.items())):
        raise ValueError("The provisional effects or fresh absence evidence differs from the reviewed scope.")
    bridge_effects(trial, effects, transaction)
    return {"status": "family_placement_rollback_verified", "preview_sha256": result["preview_sha256"],
            "project": baseline["project"], "revit_build": baseline["revit_build"],
            "action": review["action"], "effects": effects, "run_ids": run_ids}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("preview", "before", "trial", "after"):
        parser.add_argument(name)
    args = parser.parse_args()
    try:
        result = verify(*(load_json(getattr(args, name)) for name in ("preview", "before", "trial", "after")))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
