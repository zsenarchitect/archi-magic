"""Build a review-only table for an explicit Revit model cleanup selection."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from uuid import UUID

MAX_BYTES = 2 * 1024 * 1024
MAX_SELECTION = 20
PLACEMENT_KINDS = {"viewport", "schedule", "panel_schedule", "reference_view"}


def positive_id(value):
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None
            and int(value) <= 2**63 - 1)


def has_identity(value):
    return isinstance(value, str) and 0 < len(value) <= 128 and value == value.strip()


def valid_version(value):
    if not isinstance(value, str):
        return False
    try:
        version = UUID(value)
        return version.int != 0 and str(version) == value.lower()
    except ValueError:
        return False


def saved_project(document):
    return (isinstance(document, dict) and isinstance(document.get("path_sha256"), str)
            and has_identity(document.get("project_uid"))
            and has_identity(document.get("active_view_uid")) and positive_id(document.get("active_view_id"))
            and re.fullmatch(r"[0-9a-f]{64}", document.get("path_sha256") or "") is not None)


def block_reasons(row):
    reasons = []
    if not has_identity(row.get("uid")) or not valid_version(row.get("version_guid")):
        reasons.append("The element identity or version is unavailable.")
    if row.get("pinned") is not False:
        reasons.append("The element is pinned or its pin state is unknown.")
    references = row.get("references")
    if references != []:
        if isinstance(references, list) and any(edge.get("kind") in PLACEMENT_KINDS for edge in references):
            reasons.append("A sheet placement or view reference protects this item.")
        else:
            reasons.append("The item has references or the reference data is unavailable.")
    if row.get("dependent_ids") != []:
        reasons.append("The item has dependents or the dependency data is unavailable.")
    if row.get("purge_unused") is not True:
        reasons.append("Revit did not confirm this exact ID in Purge Unused.")
    if row["kind"] == "view":
        if row.get("view_type") != "DraftingView" or (row.get("content") or {}).get("status") != "none_observed":
            reasons.append("The view has content, or its contents have not been checked.")
    elif row["kind"] == "cad":
        if (row.get("linked") is not False or row.get("view_specific") is not True
                or not positive_id(row.get("owner_view_id"))):
            reasons.append("Only a view-specific import with a known owner view can be considered.")
        if row.get("owner_graphics_checked") is not True or row.get("potentially_visible_in") != []:
            reasons.append("The import may be visible, or visibility data is unavailable.")
    elif row["kind"] not in ("view_template", "filter", "scope_box"):
        reasons.append("This element category is outside the cleanup scope.")
    return reasons


def build_preview(inventory, selection):
    if inventory.get("kind") != "revit-cleanup-inventory" or inventory.get("status") not in ("reported", "incomplete"):
        raise ValueError("Use a Revit cleanup inventory that returned results.")
    if (not 1 <= len(selection) <= MAX_SELECTION or not all(map(positive_id, selection))
            or len(set(selection)) != len(selection)):
        raise ValueError("Select one to 20 unique positive decimal-string element IDs.")
    rows = inventory["rows"]
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("Expected up to 500 cleanup inventory rows.")
    by_id = {row["id"]: row for row in rows}
    if len(by_id) != len(rows) or not all(map(positive_id, by_id)):
        raise ValueError("The cleanup inventory has invalid or repeated IDs.")
    selected_ids = sorted(selection, key=int)
    scope = inventory.get("scope")
    if scope is not None and (not isinstance(scope, dict) or scope.get("selected_ids") != selected_ids
                              or set(by_id) - set(selected_ids)):
        raise ValueError("Use the same IDs as the selected Revit inventory query.")
    basis = {"inventory": inventory, "selected_ids": selected_ids}
    serialized = json.dumps(basis, ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False)
    if len(serialized) > MAX_BYTES:
        raise ValueError("The cleanup inventory exceeds 2 MiB.")
    digest = hashlib.sha256(serialized.encode("ascii")).hexdigest()

    before, after, warnings, selected, probe_candidates = [], [], [], [], []
    global_reasons = []
    document = inventory.get("document")
    if not saved_project(document):
        global_reasons.append("Read the inventory from a saved project with an active view.")
    if (inventory["status"] != "reported" or inventory.get("incomplete") != []
            or inventory.get("candidate_count") != len(rows)):
        global_reasons.append("The inventory is incomplete. Read the missing evidence before proceeding.")
    if scope is not None:
        protection = scope.get("protection_sha256")
        if not isinstance(protection, str) or re.fullmatch(r"[0-9a-f]{64}", protection) is None:
            global_reasons.append("The selected inventory lacks complete model-wide protection evidence.")
    for identifier in selected_ids:
        row = by_id.get(identifier)
        reasons = global_reasons + (block_reasons(row) if row else ["The ID is absent from the inventory."])
        kind = row["kind"] if row else "unknown"
        before.append([identifier, kind, "Selected for review"])
        after.append([identifier, kind, "Blocked" if reasons else "Needs native verification"])
        if not reasons:
            probe_candidates.append(identifier)
        for reason in reasons or ["Verify the deletion and the runtime approval policy in a disposable Revit project."]:
            warnings.append({"row": identifier, "text": reason, "blocking": True})
        if row:
            selected.append({field: row.get(field) for field in ("id", "kind", "uid", "version_guid")})
    cleanup_selection = {"document": dict(document) if saved_project(document) else None,
                         "selected_ids": selected_ids, "elements": selected, "probe_candidate_ids": probe_candidates}
    if scope is not None:
        cleanup_selection["scope"] = "selected"
    return {"kind": "change-preview", "recipe": "model-cleanup", "title": "Review selected Revit cleanup items",
            "basis_sha256": digest, "keyColumn": "Element ID", "columns": ["Element ID", "Kind", "Review"],
            "changedColumns": ["Review"], "before": before, "after": after, "warnings": warnings,
            "blocked": True, "steps": [], "review_only": True,
            "cleanup_selection": cleanup_selection}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path, help="Saved Revit cleanup query response")
    parser.add_argument("ids", nargs="+", help="One to 20 exact element IDs from the inventory")
    args = parser.parse_args()
    try:
        with args.inventory.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("The cleanup query response exceeds 2 MiB.")
        record = json.loads(raw.decode("utf-8-sig"))
        record = record.get("response", record)
        if "result" in record and record.get("status") != "ok":
            raise ValueError("The Revit query did not complete successfully.")
        preview = build_preview(record.get("result", record), args.ids)
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
        parser.exit(2, "Cleanup preview stopped: {}\n".format(error))
    json.dump(preview, sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
