"""Compare saved pyRevit room-finish results and independent model readbacks."""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys


LIMIT = 8 * 1024 * 1024
DIGEST = re.compile(r"[0-9a-f]{64}")
ELEMENT_ID = re.compile(r"[1-9][0-9]{0,18}")
CLASSES = {"floor": "Floor", "wall": "Wall", "ceiling": "Ceiling"}
OUTCOMES = {"committed": "ok", "discarded": "rejected"}
TRANSACTION_NAME = "Place reviewed room finishes"


def read(path):
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("The saved room finish evidence exceeds the read limit.")
    return json.loads(data.decode("utf-8-sig"))


def identifier(value):
    value = str(value) if type(value) is int else value
    if not isinstance(value, str) or not ELEMENT_ID.fullmatch(value) or int(value) >= 2 ** 63:
        raise ValueError("The room finish evidence has an invalid Revit ID.")
    return value


def run_record(raw, mode, status, decision, approval):
    record = raw.get("response", raw)
    if (record.get("mode") != mode or record.get("status") != status or record.get("decision") != decision
            or record.get("approval") != approval or record.get("result_truncated") is not False
            or type(record.get("exit_code")) is not int or record["exit_code"] != 0
            or not isinstance(record.get("run_id"), str) or not record["run_id"].strip()
            or record.get("error") is not None or not isinstance(record.get("result"), dict)
            or any(record.get(key) for key in (
                "failures", "dialogs", "warnings", "blocked", "closed_documents", "unclosed_documents", "reckless"))):
        raise ValueError("Use a complete successful %s response with the reviewed Revit outcome." % mode)
    return record


def effects(record):
    changes = record["changes"]
    if changes.get("other_documents") or any(changes.get(key + "_ids") for key in ("added", "modified", "deleted")):
        raise ValueError("The bridge reports effects outside the complete reviewed model scope.")
    for key in ("modified", "deleted"):
        if type(changes.get(key + "_count")) is not int or changes[key + "_count"] != 0 or changes.get(key) != []:
            raise ValueError("The bridge reports a modification or deletion outside finish creation.")
    rows = changes["added"]
    if (not isinstance(rows, list) or type(changes.get("added_count")) is not int
            or changes["added_count"] != len(rows) or len(rows) > 5200):
        raise ValueError("The bridge did not report every added finish and sketch element.")
    added = {identifier(row["id"]): row["class"] for row in rows}
    counts = changes["by_category"]
    if (len(added) != len(rows) or not all(isinstance(kind, str) and kind for kind in added.values())
            or not isinstance(counts, dict) or any(type(count) is not int or count <= 0 for count in counts.values())
            or sum(counts.values()) != len(added)):
        raise ValueError("The exact addition rows do not agree with the bridge effect counts.")
    return added


def approved_steps(plan):
    basis = plan["basis_sha256"]
    steps, rows = plan["steps"], plan["after"]
    if (plan.get("kind") != "change-preview" or plan.get("recipe") != "room-finishes"
            or plan.get("blocked") is not False or not isinstance(basis, str) or not DIGEST.fullmatch(basis)
            or not isinstance(steps, list) or not 1 <= len(steps) <= 200
            or not isinstance(rows, list) or len(steps) != len(rows)
            or plan["columns"][:3] != ["Piece", "Room ID", "Finish"]):
        raise ValueError("Use the exact approved room finish preview.")
    expected = []
    for step, row in zip(steps, rows):
        if row[:2] != [step["piece_key"], step["room_id"]] or row[2] not in CLASSES:
            raise ValueError("The approved finish rows disagree with the planned pieces.")
        if not DIGEST.fullmatch(step["piece_key"]):
            raise ValueError("The approved finish is missing its exact piece key.")
        expected.append((step["piece_key"], CLASSES[row[2]]))
    return basis, expected


def measured_batch(record, basis, expected):
    receipt = record["result"]
    if receipt.get("status") != "ok" or receipt.get("basis_sha256") != basis:
        raise ValueError("The placement receipt does not match the approved room finish preview.")
    created = receipt["created"]
    sketches = receipt["added_sketch_elements"]
    if (not isinstance(created, list) or len(created) != len(expected)
            or not isinstance(sketches, list) or len(sketches) > 5000
            or [item["id"] for item in sketches] != receipt["added_sketch_element_ids"]):
        raise ValueError("The placement receipt does not cover every planned finish and added sketch.")
    added = effects(record)
    if record["changes"].get("transactions") != [TRANSACTION_NAME]:
        raise ValueError("The effects are not from the reviewed finish transaction.")
    identities = {}
    uids = set()
    for item in created + sketches:
        element_id, uid = identifier(item["id"]), item["uid"]
        if (item["id"] != element_id or element_id in identities or not isinstance(uid, str) or not uid.strip()
                or uid in uids):
            raise ValueError("The placement receipt repeats or omits an exact Revit identity.")
        identities[element_id] = uid
        uids.add(uid)
    if set(added) != identities.keys():
        raise ValueError("The bridge additions differ from the exact placement receipt.")
    stable = []
    for item, (piece_key, expected_class) in zip(created, expected):
        shape = item["observed_sha256"]
        if (item["piece_key"] != piece_key or added[item["id"]] != expected_class
                or not isinstance(shape, str) or not DIGEST.fullmatch(shape)):
            raise ValueError("A placed finish differs from its reviewed piece, class, or measured geometry.")
        stable.append((piece_key, expected_class, shape))
    sketch_classes = Counter(added[item["id"]] for item in sketches)
    return (stable, sketch_classes), [
        {"id": key, "uid": identities[key]} for key in sorted(identities, key=int)
    ]


def query(raw):
    record = run_record(raw, "query", "ok", "rolled_back", None)
    if effects(record) or record["changes"].get("transactions") != []:
        raise ValueError("The independent Revit query reported model effects.")
    return record


def check_restored(record, basis, expected):
    if record["result"] != {"status": "rollback_verified", "basis_sha256": basis, "confirmed_absent": expected}:
        raise ValueError("The fresh query did not prove the original room scope and exact additions were restored.")


def verify(plan, dry_raw, dry_query_raw, final_raw, final_query_raw):
    basis, expected = approved_steps(plan)
    dry = run_record(dry_raw, "dry_run", "ok", "rolled_back", None)
    dry_query = query(dry_query_raw)
    dry_signature, dry_ids = measured_batch(dry, basis, expected)
    check_restored(dry_query, basis, dry_ids)

    decision = final_raw.get("response", final_raw).get("decision")
    if decision not in OUTCOMES:
        raise ValueError("Use a native Keep or Discard decision.")
    final = run_record(final_raw, "modify", OUTCOMES[decision], decision, "user")
    final_query = query(final_query_raw)
    run_ids = [record["run_id"] for record in (dry, dry_query, final, final_query)]
    if len(set(run_ids)) != 4:
        raise ValueError("Use separate pyRevit runs for each write and readback.")
    signature, final_ids = measured_batch(final, basis, expected)
    if signature != dry_signature:
        raise ValueError("The measured replay differs from the verified dry run.")
    if decision == "discarded":
        check_restored(final_query, basis, final_ids)
    else:
        receipt = final["result"]
        if final_query["result"] != {
            "status": "keep_verified", "basis_sha256": basis, "created": receipt["created"],
            "added_sketch_elements": receipt["added_sketch_elements"], "repeat_creates": 0,
        }:
            raise ValueError("The fresh Keep query did not verify the exact saved finishes and repeat behavior.")
    return {"status": "verified_room_finish_" + decision, "basis_sha256": basis,
            "run_ids": run_ids, "finish_count": len(expected)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = ("plan", "dry_run", "dry_readback", "decision", "readback")
    for name in inputs:
        parser.add_argument("--" + name.replace("_", "-"), type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(*(read(getattr(args, name)) for name in inputs)), ensure_ascii=True))
    except (OSError, AttributeError, KeyError, IndexError, TypeError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
