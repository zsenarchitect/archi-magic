# -*- coding: utf-8 -*-
"""Pure logic for detail markers on plans (issue #257, comments of 2026-11-18).

Four jobs, all from a detail_read_references.py result:

1. Duplicate by condition. A position check finds a new cut near an
   existing marker. It does not find a detail for the same condition
   (for example the same pair of floor finishes) that was cut on another
   level. find_same_condition() lists the details whose title, name or
   floor types match the requested condition.
2. Audit. A reference marker (reference section, callout or elevation)
   whose target view is on no sheet shows "-/---", and a plan filter can
   hide it. audit_references() lists each one with its host view.
3. Plan a marker change: point a marker to another view, add a reference
   section, and remove a detail view. The removal points every marker to
   another view first, or deletes the marker only when the user chose so.
   It works when the viewport was already removed by hand.
4. Revit helpers. A new section view adds helper elements (a hidden
   viewport on no sheet, sun path, work plane grid, sketch plane, extent
   element). classify_new_view_changes() names them, so a dry run needs
   no extra query to explain them.

Pure computation. Runs under CPython 3 and IronPython 2.7: no f-strings,
no type annotations, no nonlocal. No Revit imports.

Read result shape (bare, or inside the run envelope under "result"):
    {"views": [{"id", "name", "view_type", "title_on_sheet",
                "sheet": null | {"sheet_id", "sheet_number",
                                 "viewport_id", "detail_number"},
                "hidden_viewport_ids": [id, ...],
                "floor_types": [name, ...]            (optional)}],
     "references": [{"id", "kind", "host_view_id", "target_view_id"}],
     "dependents": {"<view id>": [{"id", "category", "class", "name",
                                   "owner_view_id"}]},   (optional)
     "cuts": [{"name", "floor_types": [name, ...]}]}     (optional)
"""

import re

from detail_views import unescape  # noqa: F401 (same ASCII escape as read_detail_views.py)

NO_CATEGORY = "(no category)"
SECTION_TARGETS = ("Section", "Detail", "DraftingView")
REFERENCE_HOSTS = ("FloorPlan", "CeilingPlan", "EngineeringPlan", "AreaPlan",
                   "Section", "Elevation", "Detail", "DraftingView")
KIND_NAMES = {"section": "Reference section", "callout": "Reference callout",
              "elevation": "Reference elevation"}

# Field report (issue #257): one new section view plus one viewport gave 10
# added elements. These categories are Revit helpers of a new view.
HELPER_CATEGORIES = {
    "views": "the new view and its section marker",
    "sun path": "the sun path of the new view",
    "work plane grid": "the work plane grid of the new view",
    "sheets": "the sheet that gets the new viewport",
}
# Per new view: one hidden viewport (on no sheet, SheetId -1, title "No Title")
# and up to two uncategorized helpers (the sketch plane and the extent element).
HIDDEN_VIEWPORTS_PER_VIEW = 1
UNCATEGORIZED_PER_VIEW = 2


# ---------------------------------------------------------------- read data

def load_read(data):
    """The read result dict, bare or inside the run envelope."""
    if isinstance(data, dict) and isinstance(data.get("response"), dict):
        data = data["response"]
    if isinstance(data, dict) and "views" not in data and "result" in data:
        data = data["result"]
    if not isinstance(data, dict) or "views" not in data:
        raise ValueError("The read result has no views (check status and error).")
    if data.get("escaped"):
        data = _unescape_all(data)
    return data


def _unescape_all(value):
    if isinstance(value, dict):
        return dict((k, _unescape_all(v)) for k, v in value.items())
    if isinstance(value, list):
        return [_unescape_all(v) for v in value]
    if isinstance(value, (int, float)) or value is None or isinstance(value, bool):
        return value
    return unescape(value)


def views_by_id(read):
    return dict((int(v["id"]), v) for v in read.get("views") or [])


def references_by_id(read):
    return dict((int(r["id"]), r) for r in read.get("references") or [])


def references_to(read, view_id):
    """Every reference marker that points to view_id."""
    return [r for r in read.get("references") or []
            if r.get("target_view_id") is not None and int(r["target_view_id"]) == int(view_id)]


def placement_text(view):
    """'Detail 5 on A-401', or 'on no sheet'."""
    if view is None:
        return "view missing"
    sheet = view.get("sheet")
    if not sheet:
        return "on no sheet"
    return "Detail %s on %s" % (sheet.get("detail_number") or "?", sheet.get("sheet_number") or "?")


def view_label(view, view_id=None):
    if view is None:
        return "view %s (missing)" % view_id
    return "%s (%s)" % (view.get("name") or "", view.get("id"))


# ------------------------------------------------- 1. duplicate by condition

def normalize(text):
    """Lower case words, with punctuation as spaces."""
    if text is None:
        return ""
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower()))


def _contains(haystack, phrase):
    phrase = normalize(phrase)
    if not phrase:
        return False
    return (" " + phrase + " ") in (" " + haystack + " ")


def find_same_condition(read, terms=None, floor_types=None, sheet_id=None,
                        exclude_view_ids=None):
    """Details that show the requested condition.

    terms       : words or phrases of the condition, for example the two
                  finish names ("carpet", "porcelain tile"). Order does
                  not matter.
    floor_types : floor type names at the requested cut (from the read's
                  "cuts", or given by the user).
    sheet_id    : only details on this sheet. None means every placed
                  section, detail or drafting view.

    A term matches the view name or the title on sheet, as whole words.
    A floor type matches the floor types the read found in the view.
    "same" means every given term and every given floor type matched,
    and the request had at least two of them together. "partial" means
    at least one matched. Returns {"same": [...], "partial": [...]}; each
    row is {"view_id", "name", "title_on_sheet", "placement",
    "terms_matched", "floor_types_matched", "missing"}.
    """
    terms = [t for t in (terms or []) if normalize(t)]
    wanted_floors = []
    for name in floor_types or []:
        if normalize(name) and normalize(name) not in [normalize(w) for w in wanted_floors]:
            wanted_floors.append(name)
    if not terms and not wanted_floors:
        raise ValueError("Give at least one condition term or floor type.")
    exclude = set(int(i) for i in exclude_view_ids or [])
    same, partial = [], []
    for view in read.get("views") or []:
        if view.get("view_type") not in SECTION_TARGETS or int(view["id"]) in exclude:
            continue
        sheet = view.get("sheet")
        if not sheet:
            continue
        if sheet_id is not None and int(sheet.get("sheet_id")) != int(sheet_id):
            continue
        hay = normalize(view.get("name")) + " " + normalize(view.get("title_on_sheet"))
        hits = [t for t in terms if _contains(hay, t)]
        view_floors = set(normalize(f) for f in view.get("floor_types") or [])
        floor_hits = [f for f in wanted_floors if normalize(f) in view_floors]
        if not hits and not floor_hits:
            continue
        missing = [t for t in terms if t not in hits] + [f for f in wanted_floors if f not in floor_hits]
        row = {"view_id": int(view["id"]), "name": view.get("name"),
               "title_on_sheet": view.get("title_on_sheet") or "",
               "placement": placement_text(view),
               "terms_matched": hits, "floor_types_matched": floor_hits,
               "missing": missing,
               "floor_types_read": "floor_types" in view}
        if not missing and len(terms) + len(wanted_floors) >= 2:
            same.append(row)
        else:
            partial.append(row)
    key = lambda r: (-(len(r["terms_matched"]) + len(r["floor_types_matched"])), r["placement"], r["view_id"])
    return {"same": sorted(same, key=key), "partial": sorted(partial, key=key),
            "terms": terms, "floor_types": wanted_floors}


def cut_floor_types(read, cut_name):
    """Floor types the read found along one requested cut."""
    for cut in read.get("cuts") or []:
        if cut.get("name") == cut_name:
            return list(cut.get("floor_types") or [])
    raise ValueError("The read has no cut named %r. Pass it in the read inputs under \"cuts\"." % cut_name)


def same_condition_text(found):
    out = ["Condition: %s" % ", ".join(found["terms"] + found["floor_types"])]
    if found["same"]:
        out.append("The sheet already shows this condition (%d):" % len(found["same"]))
        for r in found["same"]:
            out.append("- %s | %s | title %r" % (view_label({"name": r["name"], "id": r["view_id"]}),
                                                r["placement"], r["title_on_sheet"]))
        out.append("Recommend: point the marker to the existing detail. A new view is the second option.")
    else:
        out.append("No placed detail shows every part of this condition.")
    if found["partial"]:
        out.append("Partial matches (%d), show them to the user:" % len(found["partial"]))
        for r in found["partial"][:10]:
            out.append("- %s | %s | matched %s | missing %s" % (
                view_label({"name": r["name"], "id": r["view_id"]}), r["placement"],
                ", ".join(r["terms_matched"] + r["floor_types_matched"]) or "-",
                ", ".join(r["missing"]) or "-"))
    if found["floor_types"] and any(not r["floor_types_read"] for r in found["same"] + found["partial"]):
        out.append("NOTE: some views have no floor types in the read. Read them with sheet_id set.")
    return "\n".join(out)


# --------------------------------------------------------------- 2. audit

def audit_references(read):
    """Reference markers whose target view is on no sheet or missing.

    A half-done manual removal (the viewport deleted, the view kept)
    leaves such a marker. It shows "-/---", and a plan filter can hide
    it. Returns rows {"marker_id", "kind", "host_view_id", "host_view",
    "target_view_id", "target_view", "state"}.
    """
    views = views_by_id(read)
    rows = []
    for ref in read.get("references") or []:
        target_id = ref.get("target_view_id")
        target = views.get(int(target_id)) if target_id is not None else None
        if target is not None and target.get("sheet"):
            continue
        if target_id is None:
            state = "target unknown"
        elif target is None:
            state = "target view missing"
        else:
            state = "target on no sheet"
        host = views.get(int(ref["host_view_id"]))
        rows.append({"marker_id": int(ref["id"]), "kind": ref.get("kind"),
                     "host_view_id": int(ref["host_view_id"]),
                     "host_view": host.get("name") if host else None,
                     "target_view_id": target_id,
                     "target_view": target.get("name") if target else None,
                     "state": state})
    return sorted(rows, key=lambda r: (r["host_view"] or "", r["marker_id"]))


def audit_text(rows):
    if not rows:
        return "Every reference marker points to a view on a sheet."
    out = ["%d reference marker(s) point to a view on no sheet. Each one shows -/--- "
           "or is hidden by a plan filter:" % len(rows)]
    for r in rows:
        out.append("- %s %s in %s -> %s (%s)" % (
            KIND_NAMES.get(r["kind"], "Marker"), r["marker_id"],
            view_label({"name": r["host_view"], "id": r["host_view_id"]}) if r["host_view"] else r["host_view_id"],
            view_label({"name": r["target_view"], "id": r["target_view_id"]}) if r["target_view"]
            else r["target_view_id"], r["state"]))
    out.append("Ask the user what to do with each one: point it to a placed detail, or place the view.")
    return "\n".join(out)


def removal_state(read, view_id):
    """State of a detail view the user wants to remove.

    "placed"            : the view has a viewport on a sheet.
    "viewport removed"  : the view is on no sheet (for example the user
                          deleted the viewport by hand). Its markers
                          still point to it.
    "view missing"      : the view is not in the model.
    """
    view = views_by_id(read).get(int(view_id))
    refs = references_to(read, view_id)
    if view is None:
        state = "view missing"
    elif view.get("sheet"):
        state = "placed"
    else:
        state = "viewport removed"
    return {"state": state, "view": view, "references": refs}


# --------------------------------------------------- 3. plan marker changes

def _warn(warnings, row, text, blocking):
    warnings.append({"row": str(row), "text": text, "blocking": bool(blocking)})


def plan_changes(read, retargets=None, add_references=None, remove_view_id=None,
                 delete_markers=None, title=None):
    """A change preview for marker changes, replayed by detail_apply_markers.py.

    retargets      : [(marker_id, target_view_id)]: point a reference
                     marker to another view (ChangeReferencedView).
    add_references : [{"host_view_id", "target_view_id", "head": [x, y, z],
                       "tail": [x, y, z]}]: a new reference section.
    remove_view_id : a detail view to delete. Every marker that points to
                     it needs a decision: a retarget, or an entry in
                     delete_markers. A marker with no decision blocks the
                     preview, and the preview lists the question to ask.
    delete_markers : marker ids the user chose to delete with the view.

    Step order: retargets, new references, then the delete. A delete of
    a section view also deletes every marker that still points to it.
    """
    retargets = [(int(m), int(t)) for m, t in retargets or []]
    add_references = list(add_references or [])
    delete_markers = [int(m) for m in delete_markers or []]
    views = views_by_id(read)
    refs = references_by_id(read)
    warnings, questions, steps, deletions = [], [], [], []
    before, after = [], []
    ownership = []
    expected = set(["Views", "Viewports", "Sheets"])

    seen = set()
    for marker_id, target_id in retargets:
        if marker_id in seen:
            _warn(warnings, marker_id, "Marker %s is listed twice." % marker_id, True)
            continue
        seen.add(marker_id)
        ref = refs.get(marker_id)
        if ref is None:
            _warn(warnings, marker_id, "Marker %s is not a reference marker in the read." % marker_id, True)
            continue
        host = views.get(int(ref["host_view_id"]))
        old = views.get(int(ref["target_view_id"])) if ref.get("target_view_id") is not None else None
        new = views.get(target_id)
        if new is None:
            _warn(warnings, marker_id, "Target view %s is not in the read." % target_id, True)
            continue
        if target_id == int(ref["host_view_id"]):
            _warn(warnings, marker_id, "A marker cannot point to the view it sits in.", True)
            continue
        if remove_view_id is not None and target_id == int(remove_view_id):
            _warn(warnings, marker_id, "Marker %s would point to the view that this change deletes."
                  % marker_id, True)
            continue
        if ref.get("kind") == "section" and new.get("view_type") not in SECTION_TARGETS:
            _warn(warnings, marker_id, "A reference section can point only to a section, detail or "
                  "drafting view. %s is a %s." % (view_label(new), new.get("view_type")), True)
            continue
        if ref.get("target_view_id") is not None and int(ref["target_view_id"]) == target_id:
            _warn(warnings, marker_id, "Marker %s already points to %s. No change." % (marker_id, view_label(new)),
                  False)
            continue
        if ref.get("kind") != "section":
            _warn(warnings, marker_id, "Marker %s is a %s. Check the dry run: Revit can change its type."
                  % (marker_id, KIND_NAMES.get(ref.get("kind"), "marker").lower()), False)
        if not new.get("sheet"):
            _warn(warnings, marker_id, "%s is on no sheet. The marker will show -/---." % view_label(new), False)
        kind = KIND_NAMES.get(ref.get("kind"), "Marker")
        host_name = host.get("name") if host else str(ref["host_view_id"])
        before.append([str(marker_id), kind, host_name, view_label(old, ref.get("target_view_id")),
                       placement_text(old)])
        after.append([str(marker_id), kind, host_name, view_label(new), placement_text(new)])
        steps.append({"action": "retarget", "marker_id": marker_id,
                      "from_view_id": ref.get("target_view_id"), "to_view_id": target_id})
        ownership.append(marker_id)

    for i, spec in enumerate(add_references):
        row = "new-%d" % (i + 1)
        try:
            host_id = int(spec["host_view_id"])
            target_id = int(spec["target_view_id"])
            head = [float(c) for c in spec["head"]]
            tail = [float(c) for c in spec["tail"]]
        except (KeyError, TypeError, ValueError):
            _warn(warnings, row, "A new reference needs host_view_id, target_view_id, head and tail.", True)
            continue
        if len(head) != 3 or len(tail) != 3:
            _warn(warnings, row, "Head and tail need x, y and z (feet).", True)
            continue
        host, new = views.get(host_id), views.get(target_id)
        if host is None or new is None:
            _warn(warnings, row, "Host view %s or target view %s is not in the read." % (host_id, target_id), True)
            continue
        if host_id == target_id:
            _warn(warnings, row, "A marker cannot point to the view it sits in.", True)
            continue
        if host.get("view_type") not in REFERENCE_HOSTS:
            _warn(warnings, row, "Revit cannot add a reference section to a %s." % host.get("view_type"), True)
            continue
        if new.get("view_type") not in SECTION_TARGETS:
            _warn(warnings, row, "A reference section can point only to a section, detail or drafting view.", True)
            continue
        if abs(head[0] - tail[0]) < 1e-6 and abs(head[1] - tail[1]) < 1e-6:
            _warn(warnings, row, "Head and tail are the same point in plan.", True)
            continue
        if remove_view_id is not None and target_id == int(remove_view_id):
            _warn(warnings, row, "The new reference would point to the view that this change deletes.", True)
            continue
        before.append([row, "Reference section", host.get("name"), "-", "-"])
        after.append([row, "Reference section", host.get("name"), view_label(new), placement_text(new)])
        steps.append({"action": "add_reference", "name": row, "host_view_id": host_id,
                      "target_view_id": target_id, "head": head, "tail": tail})
        ownership.append(host_id)

    remove_state = None
    if remove_view_id is not None:
        remove_view_id = int(remove_view_id)
        remove_state = removal_state(read, remove_view_id)
        view = remove_state["view"]
        if view is None:
            _warn(warnings, remove_view_id, "View %s is not in the model. Nothing to delete." % remove_view_id,
                  True)
        else:
            moved = dict(retargets)
            for ref in remove_state["references"]:
                mid = int(ref["id"])
                host = views.get(int(ref["host_view_id"]))
                where = host.get("name") if host else str(ref["host_view_id"])
                if mid in moved and mid in delete_markers:
                    _warn(warnings, mid, "Marker %s has two decisions (point and delete)." % mid, True)
                elif mid not in moved and mid not in delete_markers:
                    questions.append({"marker_id": mid, "host_view_id": int(ref["host_view_id"]),
                                      "text": "%s %s in %s points to %s. Point it to another detail "
                                              "(Recommended), or delete it with the view?"
                                              % (KIND_NAMES.get(ref.get("kind"), "Marker"), mid, where,
                                                 view_label(view))})
            for mid in delete_markers:
                if mid not in [int(r["id"]) for r in remove_state["references"]]:
                    _warn(warnings, mid, "Marker %s does not point to the view that this change deletes."
                          % mid, True)
            if questions:
                _warn(warnings, remove_view_id, "%d marker(s) point to %s and have no decision. Deleting the "
                      "view would delete them too. Ask the user about each one." % (len(questions),
                                                                                  view_label(view)), True)
            deps = (read.get("dependents") or {}).get(str(remove_view_id))
            if deps is None:
                _warn(warnings, remove_view_id, "The read has no dependents for view %s. Read again with "
                      "dependents_of." % remove_view_id, True)
                deps = []
            dep_ids = set(int(d["id"]) for d in deps)
            if remove_view_id not in dep_ids:
                deps = [{"id": remove_view_id, "category": "Views", "class": "View",
                         "name": view.get("name") or ""}] + list(deps)
            keep = set(moved)
            for d in deps:
                did = int(d["id"])
                if did in keep:
                    continue
                category = d.get("category") or NO_CATEGORY
                name = d.get("name") or d.get("class") or ""
                if did == remove_view_id:
                    name = view.get("name") or name
                deletions.append({"id": str(did), "category": category,
                                  "name": name or "<no name>"})
                expected.add(category)
                ownership.append(did)
            for mid in delete_markers:
                if str(mid) not in [x["id"] for x in deletions]:
                    _warn(warnings, mid, "Marker %s is not in the view's dependents. Read again before you "
                          "delete." % mid, True)
            missing_refs = [int(r["id"]) for r in remove_state["references"]
                            if int(r["id"]) not in moved and int(r["id"]) not in dep_ids]
            for mid in missing_refs:
                _warn(warnings, mid, "Marker %s points to the view but is not in its dependents. Check it "
                      "in the dry run." % mid, False)
            steps.append({"action": "delete_view", "view_id": remove_view_id,
                          "expected_deleted_ids": sorted(int(x["id"]) for x in deletions)})

    notes = []
    if remove_state is not None and remove_state["view"] is not None:
        if remove_state["state"] == "viewport removed":
            notes.append("The viewport of %s was already removed. The view is still in the model, on no "
                         "sheet, and its markers still point to it." % view_label(remove_state["view"]))
        notes.append("Order: point the markers to the other view first, then delete the view. A delete "
                     "of a section view also deletes every marker that still points to it.")
    if not steps:
        _warn(warnings, "-", "The plan has no steps.", True)
    blocked = any(w["blocking"] for w in warnings)
    return {"kind": "change-preview",
            "title": title or "Change detail markers",
            "keyColumn": "Marker",
            "columns": ["Marker", "Kind", "View", "Points to", "Sheet / detail"],
            "changedColumns": ["Points to", "Sheet / detail"],
            "before": before, "after": after,
            "notes": notes, "warnings": warnings, "questions": questions,
            "blocked": blocked,
            "deletions": deletions,
            "steps": steps,
            # A reference marker reports as Views (or its marker category) in the change list.
            "edited_categories": ["Views", "Sections", "Callouts", "Elevations"],
            "expected_dependent_categories": sorted(expected),
            "ownership_ids": sorted(set(ownership))}


def plan_text(preview):
    lines = [preview["title"]]
    for b, a in zip(preview["before"], preview["after"]):
        lines.append("  %s | %s | %s | %s (%s) -> %s (%s)" % (a[0], a[1], a[2], b[3], b[4], a[3], a[4]))
    if preview["deletions"]:
        lines.append("  Delete: %d element(s), shown in full by delete_approval.py." % len(preview["deletions"]))
    for note in preview["notes"]:
        lines.append("  " + note)
    for q in preview.get("questions") or []:
        lines.append("  ASK: " + q["text"])
    for w in preview["warnings"]:
        lines.append("  %sWARNING: %s" % ("BLOCKING " if w["blocking"] else "", w["text"]))
    lines.append("  %d step(s), blocked: %s" % (len(preview["steps"]), preview["blocked"]))
    return "\n".join(lines)


# ----------------------------------------------------------------- verify

def compare(preview, actual):
    """Differences between the preview and an apply result or a fresh read.

    Apply result: {"retargeted": [[marker_id, view_id]], "added":
    [[name, marker_id, host_view_id, view_id]], "deleted": [id, ...]}.
    Fresh read: the shape of detail_read_references.py.
    """
    diffs = []
    if "views" in actual:
        read = actual
        refs = references_by_id(read)
        views = views_by_id(read)
        for st in preview["steps"]:
            if st["action"] == "retarget":
                ref = refs.get(int(st["marker_id"]))
                if ref is None:
                    diffs.append("Marker %s is gone." % st["marker_id"])
                elif int(ref.get("target_view_id") or 0) != int(st["to_view_id"]):
                    diffs.append("Marker %s points to %s, not %s." % (st["marker_id"], ref.get("target_view_id"),
                                                                     st["to_view_id"]))
            elif st["action"] == "add_reference":
                found = [r for r in read.get("references") or []
                         if int(r["host_view_id"]) == int(st["host_view_id"])
                         and int(r.get("target_view_id") or 0) == int(st["target_view_id"])]
                if not found:
                    diffs.append("No marker in view %s points to %s." % (st["host_view_id"], st["target_view_id"]))
            elif st["action"] == "delete_view":
                if int(st["view_id"]) in views:
                    diffs.append("View %s is still in the model." % st["view_id"])
                for d in preview.get("deletions") or []:
                    if int(d["id"]) in refs:
                        diffs.append("Marker %s is still in the model." % d["id"])
        return diffs
    if "result" in actual and "retargeted" not in actual:
        actual = actual["result"] or {}
    got = dict((int(m), int(v)) for m, v in actual.get("retargeted") or [])
    added = actual.get("added") or []
    want_added = [st for st in preview["steps"] if st["action"] == "add_reference"]
    for st in preview["steps"]:
        if st["action"] == "retarget":
            if got.get(int(st["marker_id"])) != int(st["to_view_id"]):
                diffs.append("Marker %s points to %s, not %s." % (st["marker_id"], got.get(int(st["marker_id"])),
                                                                 st["to_view_id"]))
        elif st["action"] == "delete_view":
            want = set(int(i) for i in st["expected_deleted_ids"])
            have = set(int(i) for i in actual.get("deleted") or [])
            if want != have:
                diffs.append("Deleted ids differ. Extra: %s. Missing: %s." % (
                    ", ".join(str(i) for i in sorted(have - want)) or "none",
                    ", ".join(str(i) for i in sorted(want - have)) or "none"))
    if len(added) != len(want_added):
        diffs.append("%d new reference(s), the preview has %d." % (len(added), len(want_added)))
    else:
        for row, st in zip(added, want_added):
            if int(row[2]) != int(st["host_view_id"]) or int(row[3]) != int(st["target_view_id"]):
                diffs.append("New reference %s is in view %s and points to %s." % (row[0], row[2], row[3]))
    return diffs


# ----------------------------------------------- 4. helpers of a new view

def _flat_by_category(changes):
    raw = changes.get("by_category") or {}
    out = {}
    nested = raw and all(isinstance(v, dict) for v in raw.values())
    parts = raw.values() if nested else [raw]
    for part in parts:
        for cat, n in part.items():
            out[cat or NO_CATEGORY] = out.get(cat or NO_CATEGORY, 0) + int(n)
    if not out:
        for kind in ("added", "modified", "deleted"):
            for row in changes.get(kind) or []:
                if isinstance(row, dict):
                    cat = row.get("category") or NO_CATEGORY
                    out[cat] = out.get(cat, 0) + 1
    return out


def classify_new_view_changes(dryrun, new_views, planned_viewports):
    """Name the changed elements of a dry run that creates new section views.

    dryrun            : the saved dry-run record (with "changes"), bare or
                        in its envelope.
    new_views         : the number of new views the preview creates.
    planned_viewports : the number of viewports the preview places.

    Returns {"available", "rows": [(category, count, kind, why)],
    "unexpected": [category, ...]}. kind is "planned", "helper" or
    "unexpected". Counts above the field report's numbers are unexpected.
    """
    if isinstance(dryrun, dict) and isinstance(dryrun.get("response"), dict):
        dryrun = dryrun["response"]
    changes = dryrun.get("changes") if isinstance(dryrun, dict) else None
    if not isinstance(changes, dict):
        return {"available": False, "rows": [], "unexpected": []}
    by_cat = _flat_by_category(changes)
    rows, unexpected = [], []
    for cat in sorted(by_cat, key=lambda c: (-by_cat[c], c)):
        n = by_cat[cat]
        low = cat.lower()
        if low == "viewports":
            extra = n - planned_viewports
            limit = HIDDEN_VIEWPORTS_PER_VIEW * new_views
            if extra <= 0:
                rows.append((cat, n, "planned", "the new viewports on the sheet"))
            elif extra <= limit:
                rows.append((cat, n, "helper", "%d planned on the sheet, plus %d hidden viewport(s) on no sheet "
                             "(SheetId -1, title \"No Title\"). Revit adds one for each new section view."
                             % (planned_viewports, extra)))
            else:
                rows.append((cat, n, "unexpected", "more viewports than planned plus one hidden viewport "
                             "for each new view"))
                unexpected.append(cat)
        elif cat == NO_CATEGORY:
            limit = UNCATEGORIZED_PER_VIEW * new_views
            if n <= limit:
                rows.append((cat, n, "helper", "the sketch plane and the extent element of each new view"))
            else:
                rows.append((cat, n, "unexpected", "more than %d uncategorized elements for %d new view(s). "
                             "Read the class of each one" % (limit, new_views)))
                unexpected.append(cat)
        elif low in HELPER_CATEGORIES:
            rows.append((cat, n, "helper" if low != "views" else "planned", HELPER_CATEGORIES[low]))
        else:
            rows.append((cat, n, "unexpected", "not a helper of a new view"))
            unexpected.append(cat)
    return {"available": True, "rows": rows, "unexpected": unexpected}


def helpers_text(summary):
    if not summary["available"]:
        return "Helpers: the dry run has no change list. Save the whole run record."
    out = ["Changed elements by category (new section views):"]
    for cat, n, kind, why in summary["rows"]:
        out.append("- %s %d (%s): %s" % (cat, n, kind, why))
    if summary["unexpected"]:
        out.append("WARN: unexpected: %s. Tell the user and ask before you apply."
                   % ", ".join(summary["unexpected"]))
    else:
        out.append("Every category is planned or a known helper of a new view.")
    return "\n".join(out)
