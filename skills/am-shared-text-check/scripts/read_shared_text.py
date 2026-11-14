# -*- coding: utf-8 -*-
"""READ step for am-shared-text-check (run_query only; it never changes the model).

Pass `workspace` = the absolute path of this skill's `scripts/` folder.

inputs (all optional):
    {"view_name": "Start"}   EVERY view with this name (any view type, not
                              templates), each with its text notes. Reading
                              every match, never only the first match, is
                              what keeps issue board notes from being missed
                              when two views share a name (issue #101).
    {"view_id": 123}         one view with its text notes
    {"all_views": true}      every non-template view with its text notes
    {"note_id": 456}         one note: full text, view id and view name

A view listing gives first lines only, so the agent can pick the notes
to read in full. Read a note in full with note_id, save it to the
scratchpad, then run shared_text_cli.py check on it.

Legacy reads escape non-ASCII text as \\uXXXX for the IronPython serializer.
For export_view mode, pass the exact project and view identity, and parse
the returned ASCII-safe JSON string once to recover the original text.
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
from bisect import bisect_right
import json
import math
import sys
import time

import System
from pyrevit.api import DB

ESCAPE = sys.platform == "cli"
MAX_EXPORT = 1024 * 1024
MAX_NOTES = 500

try:
    TEXT = basestring
except NameError:
    TEXT = str
ui_document = __revit__.ActiveUIDocument  # noqa: F821
doc = ui_document.Document if ui_document is not None else None


def txt(s):
    if not ESCAPE:
        return s
    out = []
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif ord(c) < 128:
            out.append(c)
        else:
            out.append("\\u%04x" % ord(c))
    return "".join(out)


def eid(n):
    return DB.ElementId(System.Int64(int(n)))


def eid_int(element_id):
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def split_lines(text):
    out = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                out.append(text[start:i])
                i += 2
                start = i
            else:
                out.append(text[start:i])
                i += 1
                start = i
        elif c == "\n":
            out.append(text[start:i])
            i += 1
            start = i
        else:
            i += 1
    if start < n or not out:
        out.append(text[start:])
    return out


def view_notes(view):
    notes = []
    for tn in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.TextNote):
        lines = split_lines(tn.Text)
        notes.append({"id": eid_int(tn.Id),
                      "first_line": txt(lines[0] if lines else ""),
                      "paragraphs": len(lines)})
    return {"id": eid_int(view.Id), "name": txt(view.Name),
            "type": str(view.ViewType), "text_notes": notes}


def find_views():
    if inputs.get("view_id"):  # noqa: F821
        v = doc.GetElement(eid(inputs["view_id"]))  # noqa: F821
        return [v] if v is not None and isinstance(v, DB.View) else []
    if inputs.get("all_views"):  # noqa: F821
        return [v for v in DB.FilteredElementCollector(doc).OfClass(DB.View)
                if not v.IsTemplate]
    name = (inputs.get("view_name") or "").strip().lower()  # noqa: F821
    if not name:
        return []
    return [v for v in DB.FilteredElementCollector(doc).OfClass(DB.View)
            if not v.IsTemplate and v.Name.strip().lower() == name]


def legacy_read():
    result = {}

    if inputs.get("view_id") or inputs.get("view_name") or inputs.get("all_views"):  # noqa: F821
        result["views"] = [view_notes(v) for v in find_views()]

    if inputs.get("note_id"):  # noqa: F821
        note = doc.GetElement(eid(inputs["note_id"]))  # noqa: F821
        if note is None or not isinstance(note, DB.TextNote):
            result["note"] = None
        else:
            owner = doc.GetElement(note.OwnerViewId)
            result["note"] = {"id": eid_int(note.Id),
                              "view_id": eid_int(note.OwnerViewId),
                              "view": txt(owner.Name) if owner is not None else "",
                              "text": txt(note.GetFormattedText().GetPlainText())}
    return result


def export_json_text(value):
    # The IronPython 2.7 ASCII encoder expects UTF-8 bytes represented as str.
    if isinstance(value, dict):
        return dict((key, export_json_text(item)) for key, item in value.items())
    if isinstance(value, list):
        return [export_json_text(item) for item in value]
    if isinstance(value, TEXT) and any(ord(char) > 127 for char in value):
        return value.encode("utf-8").decode("latin-1")
    return value


def export_json(value):
    if ESCAPE:
        value = export_json_text(value)
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)


def decimal_id(value):
    if not isinstance(value, TEXT) or not 0 < len(value) <= 19:
        raise ValueError("invalid_element_id")
    if any(char not in "0123456789" for char in value):
        raise ValueError("invalid_element_id")
    number = int(value)
    if number < 1 or number > 9223372036854775807 or str(number) != value:
        raise ValueError("invalid_element_id")
    return value


def finite(value):
    return not (math.isnan(value) or math.isinf(value))


def export_options(options):
    view_id = decimal_id(options.get("view_id"))
    keys = ("expected_title", "expected_path", "expected_project_uid")
    if options.get("identify_view") is not True:
        keys += ("expected_view_uid",)
    if any(not isinstance(options.get(key), TEXT) or len(options[key]) > 4096 for key in keys):
        raise ValueError("document_and_view_identity_required")
    if any(not options[key] for key in keys if key != "expected_path"):
        raise ValueError("document_and_view_identity_required")
    headings = options.get("heading_ids", [])
    if not isinstance(headings, list) or len(headings) > MAX_NOTES:
        raise ValueError("invalid_heading_ids")
    headings = [decimal_id(item) for item in headings]
    if len(set(headings)) != len(headings):
        raise ValueError("duplicate_heading_id")
    bounds = options.get("column_boundaries_mm", [])
    if not isinstance(bounds, list) or len(bounds) > 32:
        raise ValueError("invalid_column_boundaries")
    if any(isinstance(bound, (bool, TEXT)) for bound in bounds):
        raise ValueError("invalid_column_boundaries")
    bounds = [float(bound) for bound in bounds]
    if any(not finite(bound) for bound in bounds) or any(
            left >= right for left, right in zip(bounds, bounds[1:])):
        raise ValueError("invalid_column_boundaries")
    tolerance = options.get("row_tolerance_mm", 0.5)
    if isinstance(tolerance, (bool, TEXT)):
        raise ValueError("invalid_row_tolerance")
    tolerance = float(tolerance)
    if not finite(tolerance) or tolerance < 0:
        raise ValueError("invalid_row_tolerance")
    return view_id, set(headings), bounds, tolerance


def projected_position(note, origin, right, up, mm_per_foot):
    if (note.HorizontalAlignment != DB.HorizontalTextAlignment.Left
            or note.VerticalAlignment != DB.VerticalTextAlignment.Top):
        return None
    if (not note.BaseDirection.Normalize().IsAlmostEqualTo(right)
            or not note.UpDirection.Normalize().IsAlmostEqualTo(up)):
        return None
    offset = note.Coord.Subtract(origin)
    x = offset.DotProduct(right) * mm_per_foot
    y = offset.DotProduct(up) * mm_per_foot
    return (x, y) if finite(x) and finite(y) else None


def order_notes(notes, headings, bounds, tolerance):
    columns = [[] for unused in range(len(bounds) + 1)]
    for note in notes:
        columns[bisect_right(bounds, note["x_mm"])].append(note)

    ordered = []
    for col_index, column in enumerate(columns):
        column.sort(key=lambda note: (-note["y_mm"], note["x_mm"], int(note["id"])))
        rows = []
        for note in column:
            if not rows or rows[-1][0]["y_mm"] - note["y_mm"] > tolerance:
                rows.append([])
            rows[-1].append(note)
        section = None
        for row_index, row in enumerate(rows):
            if sum(note["id"] in headings for note in row) > 1:
                raise ValueError("multiple_headings_in_row")
            row.sort(key=lambda note: (note["x_mm"], int(note["id"])))
            for note in row:
                is_heading = note["id"] in headings
                if is_heading:
                    section = note["id"]
                ordered.append(dict(note, column=col_index + 1, row=row_index + 1,
                                    heading=is_heading, section_heading_id=section))
    return ordered


def export_view(options):
    try:
        view_id, headings, bounds, tolerance = export_options(options)
    except ValueError as failure:
        return {"status": "rejected", "error": str(failure)}
    except (TypeError, OverflowError):
        return {"status": "rejected", "error": "invalid_export_options"}
    if (doc is None or doc.IsFamilyDocument or doc.Title != options["expected_title"]
            or doc.PathName != options["expected_path"]
            or doc.ProjectInformation.UniqueId != options["expected_project_uid"]):
        return {"status": "rejected", "error": "document_identity_mismatch"}
    view = doc.GetElement(eid(view_id))
    if not isinstance(view, DB.ViewDrafting) or view.IsTemplate or view.Scale <= 0:
        return {"status": "rejected", "error": "drafting_view_identity_mismatch"}
    view_identity = {"id": view_id, "uid": view.UniqueId, "name": view.Name}
    if any(not isinstance(value, TEXT) or len(value) > 4096 for value in view_identity.values()):
        return {"status": "incomplete", "error": "view_identity_limit", "inventory_complete": False}
    if options.get("identify_view") is True:
        return {"status": "identified", "view": view_identity}
    if view_identity["uid"] != options["expected_view_uid"]:
        return {"status": "rejected", "error": "drafting_view_identity_mismatch"}
    payload = {
        "status": "complete", "view": view_identity,
        "inventory_complete": True, "visual_order": "verified",
        "row_tolerance_mm": tolerance, "column_boundaries_mm": bounds,
        "heading_ids": sorted(headings, key=int), "notes": [],
    }

    clock = getattr(time, "monotonic", time.time)
    deadline = clock() + 10
    inventory = payload["notes"]
    used = set()
    size = len(export_json(payload))
    try:
        origin = view.Origin
        right = view.RightDirection.Normalize()
        up = view.UpDirection.Normalize()
        valid_axes = (abs(right.DotProduct(up)) <= 1e-8
                      and abs(right.DotProduct(right) - 1) <= 1e-8
                      and abs(up.DotProduct(up) - 1) <= 1e-8)
    except Exception:
        valid_axes = False
    mm_per_foot = 304.8 / view.Scale
    collector = DB.FilteredElementCollector(doc).OwnedByView(view.Id).OfClass(DB.TextNote)
    error = None
    for note in collector:
        if clock() >= deadline:
            error = "time_limit"
            break
        if len(inventory) >= MAX_NOTES:
            error = "note_limit"
            break
        if eid_int(note.OwnerViewId) != int(view_id):
            error = "unexpected_note_owner"
            break
        identifier = str(eid_int(note.Id))
        if identifier in used:
            error = "duplicate_note_id"
            break
        used.add(identifier)
        text = note.GetFormattedText().GetPlainText()
        uid = note.UniqueId
        if not isinstance(text, TEXT) or not isinstance(uid, TEXT) or not uid:
            error = "note_text_or_identity_unavailable"
            break
        if len(text) > 65536 or len(uid) > 4096:
            error = "note_text_or_identity_limit"
            break
        if clock() >= deadline:
            error = "time_limit"
            break
        try:
            position = projected_position(note, origin, right, up, mm_per_foot) if valid_axes else None
        except Exception:
            position = None
        x, y = position if position is not None else (None, None)
        row = {"id": identifier, "uid": uid, "text": text, "x_mm": x, "y_mm": y}
        # Reserve space for ordering metadata, separators and diagnostics.
        size += len(export_json(row)) + 160
        if size > MAX_EXPORT:
            error = "output_limit"
            break
        inventory.append(row)
    if error is None and clock() >= deadline:
        error = "time_limit"
    if error:
        return {"status": "incomplete", "error": error, "inventory_complete": False}

    if headings - used:
        return {"status": "rejected", "error": "heading_not_in_view"}
    error = None
    if any(note["x_mm"] is None for note in inventory):
        error = "unsupported_note_geometry"
    else:
        try:
            payload["notes"] = order_notes(inventory, headings, bounds, tolerance)
        except ValueError as failure:
            error = str(failure)
    if error:
        payload["status"] = "incomplete"
        payload["visual_order"] = "unverified"
        payload["error"] = error
        inventory.sort(key=lambda note: int(note["id"]))
    return payload


if inputs.get("export_view") is True:  # noqa: F821
    try:
        result = export_json(export_view(inputs))  # noqa: F821
    except Exception:
        result = export_json({"status": "incomplete", "error": "native_read_failed",
                              "inventory_complete": False})
    if len(result) > MAX_EXPORT:
        result = export_json({"status": "incomplete", "error": "output_limit",
                              "inventory_complete": False})
else:
    result = legacy_read()
