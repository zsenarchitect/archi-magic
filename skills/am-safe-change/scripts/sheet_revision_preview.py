# -*- coding: utf-8 -*-
"""Preview one manually added revision across reviewed Revit sheets."""

import argparse
import hashlib
import io
import json
import re
import sys


TEXT = (str, type(u""))
LIMIT = 4 * 1024 * 1024


def positive_id(value):
    if (not isinstance(value, TEXT) or not re.match(r"^[1-9][0-9]{0,18}\Z", value)
            or int(value) >= 2**63):
        raise ValueError("Use positive decimal-string Revit IDs.")
    return value


def distinct_ids(values):
    if not isinstance(values, list):
        raise ValueError("Expected a list of revision or cloud IDs.")
    ids = [positive_id(value) for value in values]
    if len(set(ids)) != len(ids):
        raise ValueError("The reviewed scope repeats an ID.")
    return ids


def index_rows(rows):
    ids = distinct_ids([row["id"] for row in rows])
    return dict(zip(ids, rows))


def sheet_membership(sheet, catalog):
    manual = set(distinct_ids(sheet["additional_revision_ids"]))
    ordered = distinct_ids([item["revision_id"] for item in sheet["scheduled"]])
    cloud_ids = set(distinct_ids(sheet["scheduled_cloud_ids"]))
    cloud_rows = index_rows(sheet["clouds"])
    if any(type(row["scheduled"]) is not bool or type(row["revision_on_sheet"]) is not bool
           or positive_id(row["revision_id"]) not in catalog
           or row["revision_on_sheet"] != (row["revision_id"] in ordered)
           for row in cloud_rows.values()):
        raise ValueError("The sheet's revision cloud evidence is incomplete.")
    cloud_revisions = {positive_id(row["revision_id"]) for row in cloud_rows.values() if row["scheduled"] is True}
    if (cloud_ids != {key for key, row in cloud_rows.items() if row["scheduled"] is True}
            or set(ordered) != manual | cloud_revisions
            or any(value not in catalog for value in ordered)
            or ordered != sorted(ordered, key=lambda value: catalog[value]["sequence"])):
        raise ValueError("The sheet's manual revisions, scheduled clouds, and ordered schedule disagree.")
    return manual, cloud_revisions, ordered


def compute(snapshot, intent):
    if (snapshot.get("kind") != "sheet-revision-snapshot"
            or not snapshot["host_identity"]["project_uid"]
            or not re.match(r"^[0-9a-f]{64}\Z", snapshot["host_identity"]["path_sha256"])):
        raise ValueError("Use a revision snapshot from a saved project.")
    if set(intent) != {"operation", "revision_id"} or intent["operation"] not in ("add", "remove"):
        raise ValueError("Choose add or remove and one reviewed revision ID.")
    target = positive_id(intent["revision_id"])
    catalog = index_rows(snapshot["revisions"])
    if target not in catalog or not catalog[target]["uid"]:
        raise ValueError("Include the selected revision in the read snapshot.")
    sequences = [row["sequence"] for row in catalog.values()]
    if any(type(sequence) is not int or sequence <= 0 for sequence in sequences) or len(set(sequences)) != len(sequences):
        raise ValueError("The project revision sequence is unavailable.")
    sheets = index_rows(snapshot["sheets"])
    if not 1 <= len(sheets) <= 30:
        raise ValueError("Review one to 30 sheets.")
    before, after, steps, warnings = [], [], [], []
    for key in sorted(sheets, key=int):
        sheet = sheets[key]
        if not sheet["uid"]:
            raise ValueError("A reviewed sheet has no stable identity.")
        manual, clouds, scheduled = sheet_membership(sheet, catalog)
        if intent["operation"] == "add":
            proposed = manual | {target}
        else:
            proposed = manual - {target}
        expected = sorted(proposed | clouds, key=lambda value: catalog[value]["sequence"])
        label = [key, sheet["number"], sheet["name"]]
        before.append(label + [target in manual, target in scheduled])
        after.append(label + [target in proposed, target in expected])
        if proposed != manual:
            steps.append({"sheet_id": key, "sheet_uid": sheet["uid"],
                          "additional_revision_ids": sorted(proposed, key=int), "scheduled_revision_ids": expected})
        if intent["operation"] == "remove" and target in clouds:
            warnings.append({"row": key, "text": "A scheduled cloud keeps this revision on the sheet.",
                             "blocking": False})
    encoded = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return {"kind": "change-preview", "recipe": "sheet-revisions", "title": "Review sheet revision changes",
            "basis": snapshot, "basis_sha256": hashlib.sha256(encoded.encode("ascii")).hexdigest(),
            "intent": intent, "revision": catalog[target],
            "columns": ["Sheet ID", "Sheet number", "Sheet name", "Manually added", "On revision schedule"],
            "keyColumn": "Sheet ID", "changedColumns": ["Manually added", "On revision schedule"],
            "before": before, "after": after, "warnings": warnings, "blocked": False,
            "steps": steps, "ownership_ids": [step["sheet_id"] for step in steps],
            "notes": ["Revit computes the final revision numbers on each sheet after applying."]}


def unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("A JSON record repeats a field.")
    return result


def load(path):
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("The local JSON record exceeds the size limit.")
    return json.loads(raw, object_pairs_hook=unique_object)


def read_snapshot(record):
    response = record.get("response", record)
    if "result" in response:
        if response.get("status", "ok") != "ok" or response.get("error"):
            raise ValueError("The Revit revision query did not complete.")
        response = response["result"]
    if "snapshot_json" in response:
        encoded = response["snapshot_json"]
        if response["status"] != "ok" or not isinstance(encoded, TEXT) or len(encoded) > LIMIT:
            raise ValueError("The Revit revision snapshot is unavailable.")
        return json.loads(encoded, object_pairs_hook=unique_object)
    return response


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    parser.add_argument("intent")
    args = parser.parse_args(argv)
    try:
        preview = compute(read_snapshot(load(args.snapshot)), load(args.intent))
        encoded = json.dumps(preview, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2)
        if len(encoded) > LIMIT:
            raise ValueError("The revision preview exceeds the size limit.")
    except (ValueError, TypeError, KeyError, AttributeError, IOError):
        print("Sheet revision preview failed. Check the snapshot, scope, and intent.", file=sys.stderr)
        return 2
    print(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
