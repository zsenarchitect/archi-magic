"""Preview explicit text fills from a complete read-only Revit MEP query."""

import argparse
import json
import re
import sys
from uuid import UUID

from view_duplicate_preview import element_id, read_payload, read_record, snapshot_digest
from view_filter_transfer_preview import MAX_SAVED_RECORD, checked_changes, checked_record


COLUMNS = ["ElementId", "Instance UID", "Category ID", "Parameter ID", "Current state", "Text"]
BLOCKED = {
    "missing_parameter": "The shared parameter is missing.",
    "unsupported_storage": "The shared parameter does not store text.",
    "read_only": "The shared parameter is read-only.",
}


def parameter_state(row):
    if "parameter_id" not in row:
        if set(row) != {"id", "uid", "category_id", "state"}:
            raise ValueError("A missing parameter row contains contradictory evidence.")
        return "missing_parameter"
    element_id(row["parameter_id"])
    value = row["value"]
    if (type(row["has_value"]) is not bool or type(row["read_only"]) is not bool
            or (value is not None and (not isinstance(value, str) or len(value) > 4096))):
        raise ValueError("The stored parameter evidence is invalid.")
    if row["storage"] != "String":
        if value is not None:
            raise ValueError("A nontext parameter contains unexpected text evidence.")
        return "unsupported_storage"
    if row["read_only"]:
        return "read_only"
    if value and value.strip():
        return "already_filled"
    return "empty" if row["has_value"] else "unset"


def preview(saved, request):
    if not isinstance(request, dict) or set(request) != {"text"}:
        raise ValueError("Supply one explicit replacement text value.")
    text = request["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("Supply nonblank replacement text of up to 4096 characters.")
    record = checked_record(saved, "query")
    checked_changes(record, [])
    if record.get("closed_documents"):
        raise ValueError("The query closed a document. Rerun the read-only repair query.")
    evidence = read_payload(record, "repair_parameters_json")
    if evidence["kind"] != "mep-parameter-repair-evidence" or type(evidence["version"]) is not int or evidence["version"] != 1:
        raise ValueError("Use the native MEP repair evidence query.")
    guid = UUID(evidence["guid"])
    if not guid.int or str(guid) != evidence["guid"]:
        raise ValueError("The shared parameter GUID is invalid.")
    source = evidence["source"]
    host = source["host_identity"]
    if (source["document_identity"] != host or source["link_instance_id"] is not None
            or source["link_instance_uid"] is not None or not isinstance(host["project_uid"], str)
            or not host["project_uid"] or not re.fullmatch(r"[0-9a-f]{64}", host["path_sha256"])):
        raise ValueError("Use a verified standalone host project.")
    rows = evidence["elements"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        raise ValueError("Use evidence for one to 50 exact instances.")

    before, after, warnings, targets = [], [], [], []
    seen_ids, seen_uids = set(), set()
    for row in rows:
        identifier, uid = element_id(row["id"]), row["uid"]
        if (identifier in seen_ids or not isinstance(uid, str) or not uid or len(uid) > 160
                or uid in seen_uids or not re.fullmatch(r"-[1-9][0-9]{0,9}", row["category_id"])):
            raise ValueError("Use distinct and exact MEP instance identities.")
        seen_ids.add(identifier)
        seen_uids.add(uid)
        state = parameter_state(row)
        if state != row["state"]:
            raise ValueError("A repair row contradicts the stored parameter evidence.")
        previous = row.get("value")
        eligible = state in ("empty", "unset")
        fields = [identifier, uid, row["category_id"], row.get("parameter_id"), state]
        before.append(fields + [previous])
        after.append(fields + [text if eligible else previous])
        if eligible:
            targets.append(identifier)
        if state in BLOCKED:
            warnings.append({"row": identifier, "text": BLOCKED[state], "blocking": True})
    if not targets and not warnings:
        warnings.append({"text": "No selected instance has a blank writable text parameter.", "blocking": True})

    basis = snapshot_digest({"run_id": record["run_id"], "evidence": evidence, "text": text})
    return {
        "kind": "change-preview", "recipe": "mep-parameter-repair", "title": "Fill blank MEP text parameters",
        "source_run_id": record["run_id"], "basis_sha256": basis,
        "keyColumn": COLUMNS[0], "columns": COLUMNS, "changedColumns": ["Text"],
        "notes": ["Shared parameter GUID " + str(guid)], "before": before, "after": after,
        "warnings": warnings, "blocked": bool(warnings), "review_only": True,
        "steps": [], "ownership_ids": targets,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="Complete pyRevit response or result pages from one query")
    parser.add_argument("fill", help='JSON file with the approved proposal, for example {"text": "Ventilation"}')
    args = parser.parse_args()
    try:
        result = preview(read_record(args.query, MAX_SAVED_RECORD), read_record(args.fill, 65536))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        print(f"MEP repair preview stopped. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return int(result["blocked"])


if __name__ == "__main__":
    sys.exit(main())
