# -*- coding: utf-8 -*-
"""READ step for am-issue-board (run_query only; it never changes the model).

Pass `workspace` = the absolute path of this scripts/ folder, so that
`import issue_board` works.

inputs (all optional):
    {"view_id": 123}          list the text notes in this view (id, first line, size)
    {"view_name": "Board"}    same, found by view name (exact, case-insensitive)
    {"note_id": 456}          read one note: text, sha1, per-paragraph format, parsed board
    {"creator_ids": [1, 2]}   creator of each element (WorksharingUtils tooltip), for "@"
    {"counts_only": true}     with note_id: return only the parsed counts, no text

Non-ASCII text is escaped as \\uXXXX (the runtime JSON encoder is ASCII-only in
IronPython). Decode it with issue_board.unescape() before you parse or show it.
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
import sys

import System
from pyrevit.api import DB

import issue_board as ib

ESCAPE = sys.platform == "cli"
doc = __revit__.ActiveUIDocument.Document  # noqa: F821


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


def find_view():
    if inputs.get("view_id"):  # noqa: F821
        return doc.GetElement(eid(inputs["view_id"]))  # noqa: F821
    name = (inputs.get("view_name") or "").strip().lower()  # noqa: F821
    if not name:
        return None
    for v in DB.FilteredElementCollector(doc).OfClass(DB.View):
        if not v.IsTemplate and v.Name.strip().lower() == name:
            return v
    return None


def paragraph_formats(ft, text):
    out = []
    n = len(text)
    for start, line in ib.split_paragraphs(text):
        length = len(line) if len(line) > 0 else 1  # an empty paragraph: its break char
        if start >= n:
            break
        length = min(length, n - start)
        rng = DB.TextRange(start, length)
        out.append({"list": str(ft.GetListType(rng)), "indent": ft.GetIndentLevel(rng),
                    "bold": str(ft.GetBoldStatus(rng)), "underline": str(ft.GetUnderlineStatus(rng))})
    return out


result = {}

view = find_view()
if inputs.get("view_id") or inputs.get("view_name"):  # noqa: F821
    if view is None:
        result["view"] = None
    else:
        notes = []
        for tn in DB.FilteredElementCollector(doc, view.Id).OfClass(DB.TextNote):
            t = tn.Text
            first = ib.split_paragraphs(t)
            notes.append({"id": eid_int(tn.Id), "first_line": txt(first[0][1] if first else ""),
                          "paragraphs": len(first), "is_board": bool(ib.NEXT_RE.search(t))})
        result["view"] = {"id": eid_int(view.Id), "name": txt(view.Name),
                          "type": str(view.ViewType), "text_notes": notes}

if inputs.get("note_id"):  # noqa: F821
    note = doc.GetElement(eid(inputs["note_id"]))  # noqa: F821
    if note is None or not isinstance(note, DB.TextNote):
        result["note"] = None
    else:
        ft = note.GetFormattedText()
        text = ft.GetPlainText()
        formats = paragraph_formats(ft, text)
        board = ib.parse_board(text, formats)
        info = {"id": eid_int(note.Id), "view_id": eid_int(note.OwnerViewId),
                "sha1": ib.text_sha1(text), "length": len(text),
                "summary": ib.summary(board)}
        if not inputs.get("counts_only"):  # noqa: F821
            info["text"] = txt(text)
            info["formats"] = formats
            info["warnings"] = [txt(w) for w in board["warnings"]]
        result["note"] = info

if inputs.get("creator_ids"):  # noqa: F821
    creators = {}
    for n in inputs["creator_ids"]:  # noqa: F821
        try:
            tip = DB.WorksharingUtils.GetWorksharingTooltipInfo(doc, eid(n))
            creators[str(n)] = txt(tip.Creator or "")
        except Exception as e:  # not workshared, or the id is not an element
            creators[str(n)] = None
            result.setdefault("creator_errors", {})[str(n)] = txt(str(e))[:120]
    result["creators"] = creators
