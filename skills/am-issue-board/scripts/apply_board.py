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
import json

import System
from pyrevit.api import DB

import issue_board as ib

doc = __revit__.ActiveUIDocument.Document  # noqa: F821
plan = inputs["plan"]  # noqa: F821
if plan.get("kind") not in ("issue-board-plan", "mep-native-board-plan"):
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


def verify_board_target(guard, note_id, mode):
    if (mode != "edit" or ib.saved_document_identity(doc) != guard["host_identity"]
            or int(note_id) != int(guard["active"]["id"])):
        raise ValueError("The plan targets a different project or board.")
    active_text = None
    for expected in (guard["active"], guard["archive"]):
        if expected is None:
            continue
        note = doc.GetElement(eid(expected["id"]))
        if not isinstance(note, DB.TextNote) or str(note.UniqueId) != expected["unique_id"]:
            raise ValueError("The board or archive changed. Read it again and rebuild the plan.")
        text = note.GetFormattedText().GetPlainText()
        if ib.text_sha1(text) != expected["sha1"]:
            raise ValueError("The board or archive changed. Read it again and rebuild the plan.")
        if expected is guard["active"]:
            active_text = text
    return active_text


def verified_mep_edit(envelope, note_id):
    if (set(envelope) != {"kind", "version", "edit", "board_target", "source_guard"}
            or type(envelope["version"]) is not int or envelope["version"] != 1):
        raise ValueError("Unsupported native MEP board plan.")
    edit, board, guard = envelope["edit"], envelope["board_target"], envelope["source_guard"]
    if (set(edit) != {"mode", "action", "expected_sha1", "expected_length",
                      "edits", "final_text", "formats", "operations"}
            or set(board) != {"host_identity", "active", "archive"} or board["archive"] is not None
            or set(guard) != {"source", "targets", "clearance", "revit_year"}
            or not isinstance(guard["targets"], dict) or not 1 <= len(guard["targets"]) <= 100
            or board["host_identity"] != guard["source"]["host_identity"]
            or edit["expected_sha1"] != board["active"]["sha1"]):
        raise ValueError("The native MEP board plan lacks matching board and source guards.")
    board_text = verify_board_target(board, note_id, edit["mode"])
    source = guard["source"]
    targets = guard["targets"]

    # Parse the actual preview and ensure its additions reference only the
    # selected source. Linked numeric IDs cannot be host element links.
    before = ib.parse_board(board_text)["issues"]
    after = ib.parse_board(edit["final_text"])["issues"]
    added = after[len(before):]
    if (after[:len(before)] != before or not 1 <= len(added) <= 100
            or len(set(row["number"] for row in after)) != len(after)):
        raise ValueError("The native MEP board plan must only add issues.")
    link_id = source["link_instance_id"]
    allowed = {
        ("Audited element id" + key if link_id is None else
         "Linked element " + key + " in Revit link instance " + link_id): key for key in targets
    }
    matched = set()
    for row in added:
        key = allowed.get(row["findings"][0]) if row["findings"] else None
        if key is None or row["element_ids"] != ([int(key)] if link_id is None else []):
            raise ValueError("A native MEP board issue references an unexpected element.")
        matched.add(key)
    if matched != set(targets):
        raise ValueError("The native MEP board plan and selected targets differ.")

    from read_mep import run_query

    fresh = run_query(doc, {
        "source": source, "element_ids": sorted(targets, key=int),
        "link_instance_id": link_id, "link_instance_uid": source["link_instance_uid"],
        "clearance": guard["clearance"],
    })
    if fresh["status"] != "ok":
        raise ValueError("The reviewed MEP source could not be read again.")
    snapshot = json.loads(fresh["snapshot_json"])["snapshot"]
    if (snapshot["source"] != source or snapshot["revit_year"] != guard["revit_year"]
            or snapshot["clearance"] != guard["clearance"]
            or {row["id"]: row for row in snapshot["elements"]} != targets):
        raise ValueError("The reviewed MEP elements or evidence changed. Rebuild the audit and board plan.")
    return dict(edit, kind="issue-board-plan")


if plan["kind"] == "mep-native-board-plan":
    plan = verified_mep_edit(plan, inputs["note_id"])  # noqa: F821
else:
    if "source_guard" in plan:
        raise ValueError("Native MEP source guards require a native board plan.")
    if "structural_target" in plan:
        verify_board_target(plan["structural_target"], inputs["note_id"], plan["mode"])  # noqa: F821
    if "board_target" in plan:
        if plan["board_target"] is None:
            raise ValueError("Read the board and any archive with include_identity enabled, then rebuild the plan.")
        verify_board_target(plan["board_target"], inputs["note_id"], plan["mode"])  # noqa: F821

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
