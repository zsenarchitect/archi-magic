# -*- coding: utf-8 -*-
"""Build a review-only plan for explicit Revit warning repair pairs."""
from __future__ import print_function

import argparse
from collections import Counter, defaultdict
import hashlib
import io
import json
import re
import sys
from uuid import UUID

MAX_BYTES = 2 * 1024 * 1024
MAX_PAIRS = 20
MAX_WARNINGS = 5000
MAX_REFERENCES = 20000
TEXT = (str, type(u""))
REPAIR_TYPES = ("duplicate-furniture", "coincident-room-separators")


def element_id(value):
    if (not isinstance(value, TEXT) or not re.match(r"^[1-9][0-9]{0,18}\Z", value)
            or int(value) > 2**63 - 1):
        raise ValueError("Use positive decimal-string Revit element IDs.")
    return value


def failure_id(value):
    if not isinstance(value, TEXT) or str(UUID(str(value))) != value:
        raise ValueError("Use a canonical Revit warning definition GUID.")
    return value


def warning_multiset(rows):
    if not isinstance(rows, list) or len(rows) > MAX_WARNINGS:
        raise ValueError("Expected the complete inventory of up to 5,000 warnings.")
    count = 0
    result = Counter()
    for kind, failing, additional in rows:
        if not isinstance(failing, list) or not isinstance(additional, list):
            raise ValueError("The warning must include both complete ID lists.")
        count += len(failing) + len(additional)
        if count > MAX_REFERENCES:
            raise ValueError("The inventory exceeds 20,000 warning element references.")
        result[(failure_id(kind), tuple(sorted(map(element_id, failing), key=int)),
                tuple(sorted(map(element_id, additional), key=int)))] += 1
    return result


def warning_rows(counts):
    return [[kind, list(failing), list(additional)] for kind, failing, additional in sorted(counts)
            for _ in range(counts[(kind, failing, additional)])]


def snapshot_digest(snapshot):
    raw = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw) > MAX_BYTES:
        raise ValueError("The repair snapshot exceeds 2 MiB.")
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def read_payload(record, field):
    result = record["result"]
    if result.get("status") != "ok":
        raise ValueError("The native warning repair script did not complete.")
    payload = result[field]
    if not isinstance(payload, TEXT) or len(payload) > MAX_BYTES or any(ord(char) > 127 for char in payload):
        raise ValueError("Expected an ASCII warning repair payload up to 2 MiB.")
    return json.loads(payload)


def read_snapshot(record):
    return read_payload(record, "snapshot_json")


def compare_baseline(plan, current):
    normalized = dict(current, elements=dict(current["elements"]))
    borrowed = []
    for item in plan["ownership_ids"]:
        element = current["elements"][item]
        prior = plan["removal_ownership"][item]
        observed = element.get("ownership")
        if (prior is not None and observed is not None and prior[0] == "NotOwned"
                and observed[0] == "OwnedByCurrentUser" and prior[1] == observed[1]):
            normalized["elements"][item] = dict(element, ownership=prior)
            borrowed.append(item)
    if snapshot_digest(normalized) != plan["basis_sha256"]:
        raise ValueError("The approved repair plan no longer matches the model.")
    return normalized, borrowed


def compute(snapshot):
    if snapshot["kind"] != "warning-repair-snapshot" or snapshot.get("complete") is not True:
        raise ValueError("Expected a complete native warning repair snapshot.")
    digest = snapshot_digest(snapshot)
    document = snapshot["document"]
    if not document["project_uid"] or not re.match(r"^[0-9a-f]{64}\Z", document["path_sha256"]):
        raise ValueError("Read the repair candidates from a saved host project.")
    request = snapshot["request"]
    pairs = request["pairs"]
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= MAX_PAIRS:
        raise ValueError("Choose one to 20 explicit keep and remove pairs.")
    members = [element_id(pair[role]) for pair in pairs for role in ("keep_id", "remove_id")]
    if len(set(members)) != len(members):
        raise ValueError("Repair pairs must be disjoint, with different keep and remove IDs.")

    baseline = warning_multiset(snapshot["warnings"])
    allowed = {failure_id(kind): recipe for kind, recipe in snapshot["repair_types"].items()}
    if any(recipe not in REPAIR_TYPES for recipe in allowed.values()):
        raise ValueError("The snapshot reports an unknown native repair type.")
    by_member = defaultdict(set)
    for signature in baseline:
        for item in set(signature[1] + signature[2]).intersection(members):
            by_member[item].add(signature)
    rows_before, rows_after, warnings, operations = [], [], [], []
    removed_warnings = Counter()

    for pair in pairs:
        keep, remove = pair["keep_id"], pair["remove_id"]
        kind = failure_id(pair["failure_id"])
        recipe = allowed.get(kind)
        signature = (kind, tuple(sorted((keep, remove), key=int)), ())
        expected = {signature}
        kept = snapshot["elements"].get(keep) or {}
        removed = snapshot["elements"].get(remove) or {}
        reasons = []
        if not recipe or baseline[signature] != 1 or by_member[keep] != expected or by_member[remove] != expected:
            reasons.append("The pair does not have exactly one eligible warning with no other references.")
        if (not kept.get("uid") or not removed.get("uid") or kept["uid"] == removed["uid"]
                or kept.get("recipe") != recipe or removed.get("recipe") != recipe
                or kept.get("eligible") is not True or removed.get("eligible") is not True
                or not kept.get("equivalence") or kept["equivalence"] != removed.get("equivalence")):
            reasons.append("The pair does not have matching verified element state.")
        if removed.get("writable") is not True:
            reasons.append("Worksharing status blocks the requested removal.")
        if recipe == "coincident-room-separators" and snapshot.get("room_inventory_complete") is not True:
            reasons.append("The room boundary inventory is incomplete.")
        row = "repair:" + remove
        rows_before.append([row, kind, keep, remove, "Present"])
        rows_after.append([row, kind, keep, remove, "Blocked" if reasons else "Remove"])
        for reason in reasons:
            warnings.append({"row": row, "text": reason, "blocking": True})
        if not reasons:
            operations.append({"keep_id": keep, "keep_uid": kept["uid"], "remove_id": remove,
                               "remove_uid": removed["uid"], "failure_id": kind, "recipe": recipe})
            removed_warnings[signature] += 1

    blocked = bool(warnings)
    projected = baseline if blocked else baseline - removed_warnings
    before_counts, after_counts = Counter(), Counter()
    for signature, count in baseline.items():
        before_counts[signature[0]] += count
    for signature, count in projected.items():
        after_counts[signature[0]] += count
    for kind in sorted({pair["failure_id"] for pair in pairs}):
        row = "count:" + kind
        rows_before.append([row, kind, None, None, before_counts[kind]])
        rows_after.append([row, kind, None, None, after_counts[kind]])

    plan = {"kind": "warning-repair-plan", "document": document, "basis_sha256": digest, "request": request,
            "blocked": blocked, "steps": [] if blocked else operations,
            "expected_warnings": warning_rows(projected), "ownership_ids": [pair["remove_id"] for pair in pairs],
            "removal_ownership": {pair["remove_id"]: (snapshot["elements"].get(pair["remove_id"]) or {}).get("ownership")
                                  for pair in pairs}}
    display = {"kind": "change-preview", "recipe": "warning-repair", "title": "Review Revit warning repairs",
               "basis_sha256": digest, "keyColumn": "Row", "columns": [
                   "Row", "Warning type", "Keep element ID", "Removal element ID", "Action or warning count"],
               "changedColumns": ["Action or warning count"], "before": rows_before, "after": rows_after,
               "warnings": warnings, "blocked": blocked, "steps": [], "review_only": True,
               "ownership_ids": plan["ownership_ids"]}
    return plan, display


def load_record(path):
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        raw = stream.read(2 * MAX_BYTES + 65537)
    if len(raw) > 2 * MAX_BYTES + 65536:
        raise ValueError("The full Revit record exceeds the supported size.")
    data = json.loads(raw)
    return data.get("response", data)


def check_runtime_receipt(plan, record, outcome):
    expected_status = "rejected" if outcome == "discarded" else "ok"
    if (record.get("decision") != outcome or record.get("status") != expected_status
            or (outcome == "discarded" and record.get("approval") != "user")
            or (outcome == "committed" and record.get("approval") not in ("user", "auto"))):
        raise ValueError("The full pyRevit record does not establish the requested repair outcome.")
    if any(record.get(field) for field in ("error", "failures", "dialogs", "blocked", "warnings",
                                           "unclosed_documents")):
        raise ValueError("The pyRevit record reports a failure, dialog or other unresolved effect.")
    if plan.get("kind") != "warning-repair-plan" or plan.get("blocked") is not False or not plan["steps"]:
        raise ValueError("Use an unblocked reviewed warning repair plan.")
    receipt = read_payload(record, "receipt_json")
    expected = sorted((step["remove_id"] for step in plan["steps"]), key=int)
    changes = record["changes"]
    deleted = changes["deleted"]
    if (not isinstance(deleted, list) or any(type(item) not in (int, type(u""), str) for item in deleted)
            or type(changes["deleted_count"]) is not int
            or changes["deleted_count"] != len(expected)
            or sorted((element_id(str(item)) for item in deleted), key=int) != expected
            or changes["added_count"] != 0 or changes["modified_count"] != 0
            or changes.get("other_documents") or changes.get("added") or changes.get("modified")
            or changes.get("by_category")):
        raise ValueError("The runtime change record does not contain exactly the approved deletions.")
    if (receipt["basis_sha256"] != plan["basis_sha256"] or receipt["document"] != plan["document"]
            or receipt["deleted"] != expected or receipt["warnings"] != plan["expected_warnings"]):
        raise ValueError("The inner transaction receipt differs from the approved repair plan.")
    if any(not re.match(r"^[0-9a-f]{64}\Z", receipt[field]) for field in ("kept_sha256", "rooms_sha256")):
        raise ValueError("The inner transaction did not provide survivor and room evidence.")
    return receipt


def verify_outcome(command, plan, record, fresh):
    outcomes = {"verify-dry-run": "rolled_back", "verify-discard": "discarded", "verify-keep": "committed"}
    receipt = check_runtime_receipt(plan, record, outcomes[command])
    changes = fresh["changes"]
    if (fresh.get("status") != "ok" or fresh.get("decision") != "rolled_back"
            or any(fresh.get(field) for field in ("error", "failures", "dialogs", "blocked", "warnings",
                                                  "unclosed_documents"))
            or any(type(changes[field]) is not int or changes[field] != 0
                   for field in ("added_count", "modified_count", "deleted_count"))
            or changes.get("other_documents")):
        raise ValueError("The independent Revit query did not finish without changes.")
    if command == "verify-keep":
        observed = read_payload(fresh, "readback_json")
        expected = {field: receipt[field] for field in
                    ("document", "basis_sha256", "deleted", "kept_sha256", "rooms_sha256", "warnings")}
        if observed != expected:
            raise ValueError("The independent Revit readback differs from the kept transaction receipt.")
        return {"status": "verified_keep", "deleted": receipt["deleted"],
                "keepers": [{"id": step["keep_id"], "uid": step["keep_uid"]} for step in plan["steps"]]}
    _, borrowing = compare_baseline(plan, read_snapshot(fresh))
    return {"status": "verified_dry_run" if command == "verify-dry-run" else "verified_discard",
            "borrowed_removal_ids": borrowing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", help="Read record, verify-dry-run, verify-discard, or verify-keep")
    parser.add_argument("plan", help="Plan output or reviewed plan")
    parser.add_argument("preview", help="Preview output or full repair run record")
    parser.add_argument("--fresh", help="Independent Revit query record for verification")
    args = parser.parse_args()
    try:
        if args.record in ("verify-dry-run", "verify-discard", "verify-keep"):
            if not args.fresh:
                raise ValueError("Supply the full independent Revit query record with --fresh.")
            report = verify_outcome(args.record, load_record(args.plan), load_record(args.preview), load_record(args.fresh))
            print(json.dumps(report, ensure_ascii=True, indent=2))
            return 0
        plan, display = compute(read_snapshot(load_record(args.record)))
        for path, data in [(args.plan, plan), (args.preview, display)]:
            with io.open(path, "w", encoding="utf-8") as stream:
                stream.write(u"%s\n" % json.dumps(data, ensure_ascii=True, indent=2))
        return 1 if plan["blocked"] else 0
    except (ValueError, KeyError, TypeError, AttributeError, IOError) as error:
        print("Warning repair preview stopped: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
