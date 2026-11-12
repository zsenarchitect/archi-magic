"""Verify a Revit split trial against independent before and after queries and bridge effects."""

import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

import element_split_preview as planner
from verify_element_split import read_json, verify as verify_pieces


EFFECTS = ("added", "modified", "deleted")
MAX_IDS = 1002
MAX_BRIDGE_BYTES = 8 * 1024 * 1024
TRANSACTION = re.compile(r"Archi Magic element split trial [0-9a-f]{32}")
AUDIT = re.compile(r"[0-9a-f]{32}")


def digest(value):
    return sha256(value.encode("utf-8")).hexdigest()


def decode(text):
    if not isinstance(text, str) or len(text.encode("utf-8")) > planner.MAX_BYTES:
        raise ValueError("The saved result does not contain complete split evidence.")
    return json.loads(text, parse_float=Decimal, object_pairs_hook=planner.exact_object)


def exact_ids(values):
    ids = [planner.identifier(str(value) if type(value) is int else value)
           for value in planner.bounded_list(values, 0, MAX_IDS, "effect IDs")]
    if len(set(ids)) != len(ids):
        raise ValueError("The saved effects repeat an element identity.")
    return set(ids)


def run_record(raw, mode):
    record = raw.get("response", raw)
    if (record.get("mode") != mode or record.get("status") != "ok"
            or record.get("decision") not in ("rolled_back", "no_changes")
            or type(record.get("exit_code")) is not int or record["exit_code"] != 0
            or record.get("result_truncated") is not False or record.get("approval") is not None
            or record.get("error") is not None or record.get("engine", {}).get("requested") != "cpython"
            or any(record.get(key) for key in ("failures", "dialogs", "warnings", "blocked", "unclosed_documents"))
            or not isinstance(record.get("result"), dict)):
        raise ValueError("Use complete, successful CPython bridge results with the expected mode.")
    planner.uid(record["run_id"])
    return record


def bridge_effects(record):
    changes = record["changes"]
    if changes.get("other_documents"):
        raise ValueError("The bridge reports effects in another document.")
    effects = {}
    for key in EFFECTS:
        rows = changes[key]
        if (not isinstance(rows, list) or type(changes[key + "_count"]) is not int
                or len(rows) != changes[key + "_count"] or changes.get(key + "_ids")):
            raise ValueError("The saved bridge record does not contain the full element effect list.")
        effects[key] = exact_ids(rows if key == "deleted" else [row["id"] for row in rows])
    categories = changes["by_category"]
    if (not isinstance(categories, dict) or any(type(count) is not int or count <= 0 for count in categories.values())
            or sum(categories.values()) != len(effects["added"]) + len(effects["modified"])):
        raise ValueError("The bridge category counts disagree with the exact effect rows.")
    return effects, changes["transactions"]


def accumulate(events):
    # pyRevit AgentChangeSet.Record accumulates added, deleted and modified in this order.
    added, modified, deleted = set(), set(), set()
    for event in events:
        added.update(event["added"])
        deleted.update(event["deleted"] - added)
        added.difference_update(event["deleted"])
        modified.difference_update(event["deleted"])
        modified.update(event["modified"] - added)
    return {"added": added, "modified": modified, "deleted": deleted}


def audit(record):
    effects, transactions = bridge_effects(record)
    result = record["result"]
    controls = decode(result["controls_json"])
    if (any(effects.values()) or transactions != [] or result.get("status") != "ok"
            or AUDIT.fullmatch(result["audit_token"]) is None or not isinstance(controls, dict) or not controls):
        raise ValueError("Use an independent clean query with unrelated control evidence.")
    return result


def verify(before_raw, trial_raw, after_raw, approval, approval_sha, cascade=None, cascade_sha=None):
    before, trial, after = [run_record(record, mode) for record, mode in (
        (before_raw, "query"), (trial_raw, "dry_run"), (after_raw, "query"))]
    run_ids = [row["run_id"] for row in (before, trial, after)]
    if len(set(run_ids)) != 3:
        raise ValueError("Use separate bridge runs for the baseline, trial, and fresh rollback audit.")
    baseline, fresh = audit(before), audit(after)
    native = trial["result"]
    receipt = decode(native["trial_json"])
    name = receipt["transaction"]
    if (native["status"] != "trial_rolled_back" or receipt["status"] != "trial_rolled_back"
            or TRANSACTION.fullmatch(name) is None or receipt["approval_sha256"] != approval_sha
            or receipt["baseline_audit_token"] != baseline["audit_token"] or fresh["audit_token"] == baseline["audit_token"]
            or baseline["trial_transaction"] is not None or baseline["confirmed_absent_ids"] != []
            or fresh["trial_transaction"] != name
            or any(baseline[key] != fresh[key] for key in ("snapshot_json", "inventory_json", "controls_json"))
            or any(receipt[field] != digest(baseline[key]) for field, key in (
                ("snapshot_sha256", "snapshot_json"), ("inventory_sha256", "inventory_json"),
                ("controls_sha256", "controls_json")))):
        raise ValueError("The baseline, trial, and fresh audit do not describe the same reviewed fixture.")
    snapshot = decode(baseline["snapshot_json"])
    inventory = decode(baseline["inventory_json"])
    measured = decode(receipt["readback_json"])
    comparison = verify_pieces(snapshot, receipt["snapshot_sha256"], inventory, receipt["inventory_sha256"],
                               approval, measured)
    if comparison["status"] != "matched" or comparison != receipt["comparison"]:
        raise ValueError("The saved result pieces do not match the independently recomputed preview.")

    inner = receipt["inner_effects"]
    planner.fields(inner, set(EFFECTS))
    inner = {key: exact_ids(inner[key]) for key in EFFECTS}
    sources = {item["id"] for item in snapshot["elements"]}
    result_ids = {item["id"] for item in measured["pieces"]}
    controls = set(decode(baseline["controls_json"]))
    if any(controls & values for values in inner.values()) or controls & sources:
        raise ValueError("A supposed unrelated control is inside the split effects.")
    if exact_ids(fresh["confirmed_absent_ids"]) != inner["added"]:
        raise ValueError("The fresh query did not check the exact trial additions.")
    is_floor = any(item["category"] == "floor" for item in snapshot["elements"])
    if is_floor:
        source = snapshot["elements"][0]
        if (len(sources) != 1 or cascade is None or receipt.get("floor_cascade_sha256") != cascade_sha
                or cascade.get("kind") != "element-split-floor-cascade" or cascade["host_identity"] != snapshot["host_identity"]
                or cascade["source"] != {"id": source["id"], "uid": source["uid"]}
                or cascade["sketch"]["id"] not in inner["deleted"]
                or any(cascade[key] != receipt[key] for key in ("snapshot_sha256", "inventory_sha256"))
                or exact_ids([row["id"] for row in cascade["deleted"]]) != inner["deleted"]
                or not sources <= inner["deleted"] or not result_ids <= inner["added"] or inner["modified"]
                or inner["added"] & inner["deleted"]):
            raise ValueError("The floor effects do not match the separately reviewed deletion cascade.")
    elif (cascade is not None or receipt.get("floor_cascade_sha256") is not None or inner["deleted"]
          or inner["modified"] != sources or inner["added"] != result_ids - sources or not sources <= result_ids):
        raise ValueError("The wall or column effects do not match the measured pieces.")

    events = []
    group_rollbacks = 0
    for event in planner.bounded_list(receipt["rollback_events"], 1, 8, "rollback events"):
        planner.fields(event, {"operation", "transactions", *EFFECTS})
        if (event["operation"] not in ("TransactionRolledBack", "TransactionGroupRolledBack")
                or event["transactions"] not in ([], [name])):
            raise ValueError("The native receipt reports an unrelated rollback operation.")
        group_rollbacks += int(event["operation"] == "TransactionGroupRolledBack")
        effects = {key: exact_ids(event[key]) for key in EFFECTS}
        if (effects["added"] - inner["deleted"] or effects["deleted"] - inner["added"]
                or effects["modified"] - (inner["modified"] | inner["deleted"])):
            raise ValueError("A rollback event contains unreviewed element IDs.")
        events.append(effects)
    actual, transactions = bridge_effects(trial)
    expected = accumulate([inner, *events])
    decision = "rolled_back" if any(expected.values()) else "no_changes"
    if group_rollbacks != 1 or actual != expected or transactions != [name] or trial["decision"] != decision:
        raise ValueError("The complete bridge effect record disagrees with the native trial and rollback events.")

    return {"status": "verified_split_trial_rollback", "run_ids": run_ids, "transaction": name,
            "approval_sha256": approval_sha, "snapshot_sha256": receipt["snapshot_sha256"],
            "controls_sha256": receipt["controls_sha256"],
            "inner_effects": {key: sorted(ids, key=int) for key, ids in inner.items()},
            "bridge_effects": {key: sorted(ids, key=int) for key, ids in actual.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before", "trial", "after", "approval"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--floor-cascade", type=Path)
    args = parser.parse_args()
    try:
        before, trial, after = (read_json(path, MAX_BRIDGE_BYTES)[0] for path in (args.before, args.trial, args.after))
        approval, approval_sha = read_json(args.approval)
        cascade, cascade_sha = read_json(args.floor_cascade) if args.floor_cascade else (None, None)
        result = verify(before, trial, after, approval, approval_sha, cascade, cascade_sha)
    except (ArithmeticError, AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        print(f"Split rollback verification stopped. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
