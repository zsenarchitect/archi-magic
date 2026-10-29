# -*- coding: utf-8 -*-
"""WRITE step for am-issue-board (run_modify: dry_run=true first, then the real run).

Pass `workspace` = the absolute path of this scripts/ folder, so that
`import issue_board` works. The plan comes from board_cli.py (unchanged).

inputs:
    create: {"plan": <plan mode "create">, "view_id": 123, "x": 0.0, "y": 0.0,
             "width_ft": 0.5, "text_type_id": 789 (optional: default text type)}
    edit:   {"plan": <plan mode "edit">, "note_id": 456}

Edit mode re-reads the note and stops with no change when its text is not the
text the plan was made from (sha1 guard), or when an anchor paragraph is not
at its planned offset. It edits only the planned paragraphs with Find +
SetPlainText, so manual edits elsewhere in the note stay.

result = {"status": "ok" | "stopped", "message": ..., "note_id": n,
          "format_ops": n, "paragraphs": n, "sha1_after": ...}
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
import System
from pyrevit.api import DB

import issue_board as ib

doc = __revit__.ActiveUIDocument.Document  # noqa: F821
plan = inputs["plan"]  # noqa: F821
if plan.get("kind") != "issue-board-plan":
    raise ValueError("inputs.plan is not an issue-board plan")


def eid(n):
    return DB.ElementId(System.Int64(int(n)))


def eid_int(element_id):
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def make_range(start, length):
    return DB.TextRange(start, length)


def list_type(name):
    # "None" is a Python keyword, so read that enum member with getattr.
    return DB.ListType.Bullet if name == "bullet" else getattr(DB.ListType, "None")


result = {"status": "ok"}
t = DB.Transaction(doc, "Issue board: %s" % plan.get("action", plan["mode"]))
t.Start()
try:
    if plan["mode"] == "create":
        view = doc.GetElement(eid(inputs["view_id"]))  # noqa: F821
        type_id = inputs.get("text_type_id")  # noqa: F821
        type_id = eid(type_id) if type_id else doc.GetDefaultElementTypeId(DB.ElementTypeGroup.TextNoteType)
        origin = DB.XYZ(float(inputs.get("x", 0.0)), float(inputs.get("y", 0.0)), 0.0)  # noqa: F821
        opts = DB.TextNoteOptions(type_id)
        note = DB.TextNote.Create(doc, view.Id, origin, float(inputs.get("width_ft", 0.5)),  # noqa: F821
                                  plan["final_text"], opts)
        ft = note.GetFormattedText()
        result["format_ops"] = ib.apply_formats(ft, plan["formats"], make_range, list_type)
        note.SetFormattedText(ft)
    else:
        note = doc.GetElement(eid(inputs["note_id"]))  # noqa: F821
        if note is None or not isinstance(note, DB.TextNote):
            raise ib.BoardChanged("The note is not in the model any more.")
        ft = note.GetFormattedText()
        result["format_ops"] = ib.apply_plan(ft, plan, make_range, list_type)
        note.SetFormattedText(ft)
    after = note.GetFormattedText().GetPlainText()
    result["note_id"] = eid_int(note.Id)
    result["paragraphs"] = len(ib.split_paragraphs(after))
    result["sha1_after"] = ib.text_sha1(after)
    t.Commit()
except (ib.BoardChanged, ib.BoardError) as e:
    t.RollBack()
    result = {"status": "stopped", "message": str(e)}
