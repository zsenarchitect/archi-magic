# -*- coding: utf-8 -*-
"""Pure computation for the detail-views recipe (issue #50).

Field report: new section detail views on a sheet got odd viewport label
settings, landed about 1.4 ft from the requested point, and took the next
free detail number instead of the next number in the sheet's pattern.

This module is pure computation. It runs under CPython 3 and IronPython
2.7: no f-strings, no type annotations, no nonlocal. No Revit imports.

Read result shape (from read_detail_views.py, bare or inside the run
envelope under "result"):
    {"document": <title>,
     "sheets": [{"id", "number", "name",
                  "viewports": [{"id", "view_id", "view_name",
                                 "detail_number", "viewport_type_id",
                                 "label_offset": [x, y],
                                 "label_line_length": number,
                                 "box_center": [x, y, z]}, ...]}, ...],
     "sections": [{"id", "name"}, ...]}

A detail number is an integer. Text that is not an integer is skipped,
and the preview warns about it.
"""

import json
import re


def parse_detail_number(text):
    """An integer detail number, or None when the text is not an integer."""
    if text is None:
        return None
    if isinstance(text, int):
        return text
    text = str(text).strip()
    if re.match(r"^-?\d+$", text):
        return int(text)
    return None


def propose_detail_numbers(existing, count):
    """Propose detail numbers that follow the sheet's own pattern.

    existing : detail numbers already on the sheet (ints).
    count    : how many new numbers are needed.

    Rule, from the field report (existing 10-12, 20-21, two new
    details -> 22, 30):
    - The first new number continues the highest run: max + 1.
    - Each later new number starts the next decade: the smallest
      multiple of 10 above the previous proposal.
    With no existing numbers, the first proposal is 1, then 10, 20, ...

    This rule is a proposal, not a fact about the sheet. The preview
    always shows it, and step 6 lets the user change it.
    """
    nums = sorted(set(int(n) for n in existing))
    out = []
    if not nums:
        nxt = 1
    else:
        nxt = nums[-1] + 1
    for i in range(count):
        if i > 0:
            nxt = ((nxt // 10) + 1) * 10
        out.append(nxt)
    return out


def load_read_result(data):
    """The read result dict, bare or inside the run envelope."""
    if "result" in data and "sheets" not in data:
        data = data["result"]
    if data is None or "sheets" not in data:
        raise ValueError("The read result has no sheets (check status and error).")
    return data


def unescape(text):
    """Reverse the IronPython ASCII escaping (see read_detail_views.py)."""
    try:
        _chr = unichr
    except NameError:
        _chr = chr
    if text is None:
        return text
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] == "u":
            try:
                out.append(_chr(int(text[i + 2:i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        if c == "\\" and i + 1 < len(text) and text[i + 1] == "\\":
            out.append("\\")
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _fmt_offset(offset):
    return "%.2f, %.2f" % (offset[0], offset[1])


def _fmt_center(center):
    return "%.2f, %.2f" % (center[0], center[1])


def compute_detail_plan(read, sheet_id, reference_id, details, title=None):
    """Build the change preview for new detail views on one sheet.

    read         : the read result dict (see module docstring).
    sheet_id     : element id of the target sheet.
    reference_id : element id of the reference viewport. Its label
                   settings are copied to every new viewport.
    details      : list of dicts, one per new detail view:
                       {"name": str,
                        "x": float, "y": float,   (requested box center, feet)
                        "source_view_id": int,    (section to copy bbox/template from)
                        "source_view_name": str}
    title        : preview title. Default names the sheet.

    Returns the change-preview dict (same shape as the other recipes).
    "before" rows hold "-" for values that do not exist yet, because the
    viewports are new. "steps" is the ordered list the apply script
    replays. "blocked" is true when a blocking warning exists.
    """
    sheets = dict((s["id"], s) for s in read.get("sheets", []))
    sheet = sheets.get(sheet_id)
    warnings = []
    blocked = False

    def warn(text, blocking):
        warnings.append({"row": "", "text": text, "blocking": blocking})
        return blocking

    if sheet is None:
        warn("Sheet %s is not in the read result." % sheet_id, True)
        return _blocked_preview(title or "Add detail views", warnings)
    viewports = sheet.get("viewports", [])
    reference = None
    for vp in viewports:
        if vp["id"] == reference_id:
            reference = vp
            break
    if reference is None:
        warn("Reference viewport %s is not on sheet %s." % (reference_id, sheet["number"]), True)
        return _blocked_preview(title or "Add detail views", warnings)
    if reference.get("label_offset") is None or reference.get("label_line_length") is None:
        warn("Reference viewport %s has no label settings to copy." % reference_id, True)
        return _blocked_preview(title or "Add detail views", warnings)
    if reference.get("viewport_type_id") is None:
        warn("Reference viewport %s has no viewport type to copy." % reference_id, True)
        return _blocked_preview(title or "Add detail views", warnings)

    existing = []
    for vp in viewports:
        n = parse_detail_number(vp.get("detail_number"))
        if n is None and vp.get("detail_number") not in (None, ""):
            warnings.append({"row": vp.get("view_name", ""),
                             "text": "Detail number %r is not an integer; it is skipped." % vp.get("detail_number"),
                             "blocking": False})
        elif n is not None:
            existing.append(n)
    sections = dict((s["id"], s) for s in read.get("sections", []))

    proposed = propose_detail_numbers(existing, len(details))
    for n in proposed:
        if n in existing:
            warn("Proposed detail number %d is already used on sheet %s." % (n, sheet["number"]), True)

    before = []
    after = []
    steps = []
    for detail, number in zip(details, proposed):
        src = sections.get(detail["source_view_id"])
        if src is None:
            warn("Source section %s is not in the read result." % detail["source_view_id"], True)
            continue
        center = [detail["x"], detail["y"], 0.0]
        before.append([detail["name"], "-", "-", "-", "-"])
        after.append([detail["name"], number, _fmt_offset(reference["label_offset"]),
                      "%.2f" % reference["label_line_length"], _fmt_center(center)])
        steps.append({"name": detail["name"],
                      "sheet_id": sheet_id,
                      "source_view_id": detail["source_view_id"],
                      "viewport_type_id": reference["viewport_type_id"],
                      "detail_number": number,
                      "label_offset": list(reference["label_offset"]),
                      "label_line_length": reference["label_line_length"],
                      "box_center": center})
    for w in warnings:
        if w["blocking"]:
            blocked = True
    notes = ["%d new detail view(s) on sheet %s." % (len(steps), sheet["number"]),
             "Label settings are copied from viewport %s." % reference.get("view_name", reference_id),
             "Detail numbers follow the sheet pattern, not the next free number."]
    return {"kind": "change-preview",
            "title": title or "Add detail views to sheet %s" % sheet["number"],
            "keyColumn": "Detail view",
            "columns": ["Detail view", "Detail number", "Label offset", "Label line length", "Box center"],
            "changedColumns": ["Detail number", "Label offset", "Label line length", "Box center"],
            "before": before,
            "after": after,
            "notes": notes,
            "warnings": warnings,
            "blocked": blocked,
            "params": {"sheet_id": sheet_id, "reference_id": reference_id,
                       "label_offset": list(reference["label_offset"]),
                       "label_line_length": reference["label_line_length"]},
            "steps": steps,
            "expected_dependent_categories": ["Views"]}


def _blocked_preview(title, warnings):
    return {"kind": "change-preview",
            "title": title,
            "keyColumn": "Detail view",
            "columns": ["Detail view", "Detail number", "Label offset", "Label line length", "Box center"],
            "changedColumns": ["Detail number", "Label offset", "Label line length", "Box center"],
            "before": [],
            "after": [],
            "notes": [],
            "warnings": warnings,
            "blocked": True,
            "params": {},
            "steps": [],
            "expected_dependent_categories": ["Views"]}


def text_table(preview):
    """The chat table for a preview, in the other recipes' style."""
    lines = [preview["title"]]
    for row in preview["after"]:
        lines.append("  %s | %s | %s | %s | %s" % tuple(row))
    for note in preview["notes"]:
        lines.append("  " + note)
    for w in preview["warnings"]:
        lines.append("  %sWARNING: %s" % ("BLOCKING " if w["blocking"] else "", w["text"]))
    lines.append("  %d new viewport(s), blocked: %s" % (len(preview["after"]), preview["blocked"]))
    return "\n".join(lines)


def format_actual_row(row):
    """One apply-result row as the preview's after-row strings.

    Apply rows are [name, number, [ox, oy], line_length, [cx, cy]].
    """
    return [row[0], row[1], _fmt_offset(row[2]), "%.2f" % row[3], _fmt_center(row[4])]


def compare(preview, rows):
    """Differences between the preview's after-rows and actual rows.

    rows are apply-result rows (see format_actual_row). Returns a list
    of difference strings; empty means the actual result matches.
    """
    actual = dict((r[0], format_actual_row(r)) for r in rows)
    diffs = []
    for want in preview["after"]:
        got = actual.get(want[0])
        if got is None:
            diffs.append("%s: missing from the actual result" % want[0])
            continue
        if [str(x) for x in got] != [str(x) for x in want]:
            diffs.append("%s: preview %s, actual %s" % (want[0], want, got))
    return diffs


def preview_counts(preview):
    return {"changed": len(preview["after"]),
            "warnings": len(preview["warnings"])}
