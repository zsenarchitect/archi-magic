# -*- coding: utf-8 -*-
"""am-workset-audit, step 1: READ-ONLY workset audit (run with run_query).

Runs on the pyRevit Agent Runtime, IronPython 2.7 (no f-strings) or CPython 3.
Adapted from Utopia5327/claude-plugin-for-revit-bim skills/workset-manager (MIT),
rewritten for pyRevit (no Dynamo DocumentManager / IN / OUT).

inputs (all optional):
  rules                 list of rule ids to apply.
                          Default: ["links", "cad_links", "levels_grids", "workset1"]
                          "links"         Revit link instances not on the links workset
                          "cad_links"     CAD links and imports (ImportInstance) not on the CAD
                                          links workset. View-specific CAD is skipped: its
                                          workset is the view workset and cannot be changed.
                          "levels_grids"  levels and grids not on the levels-and-grids workset
                          "workset1"      model elements left on the default workset (report only)
                          "categories"    elements whose category is in category_map but sit
                                          on another workset
  links_workset         target workset for links: workset id (int, preferred) or exact name
  cad_links_workset     target workset for CAD links: workset id or exact name. Default: the one
                          user workset whose name has the word "CAD" (not "Facade")
  levels_grids_workset  target workset for levels and grids: workset id or exact name
  default_workset       default workset: id or name. Default: "Workset1"
  category_map          {"OST_Walls": 1234 or "Exterior", ...}: BuiltInCategory name -> workset
  Workset ids are in the "worksets" list of a first run. Ids avoid any problem with
  non-ASCII workset names.
  max_items             cap on listed findings. Default: 500

result: worksets with element counts, findings, and "moves" (only movable elements) in the
exact shape that workset_fix.py takes as its "moves" input. "cad_links_skipped" lists the
view-specific CAD elements that the cad_links rule does not move.

The pure functions (name_has, match_workset, split_cad) use no Revit API, so the tests in
../tests can import this file outside Revit.
"""
import re

try:
    from pyrevit.api import DB
except ImportError:  # outside Revit (tests). The audit itself runs only when DB exists.
    DB = None

try:
    inputs
except NameError:
    inputs = {}
inputs = inputs or {}


def eid_int(element_id):
    """ElementId -> int. Revit 2024+ has .Value; .IntegerValue is gone in 2026."""
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def name_has(name, hint, word=False):
    """Case-insensitive test. word=True needs the hint as a whole word, so "cad" matches
    "3_CAD Links" and "Links-CAD" but not "Facade" or "Arcade"."""
    name = name.lower()
    if not word:
        return hint in name
    return re.search(r"(?<![a-z])" + re.escape(hint) + r"(?![a-z])", name) is not None


def match_workset(pairs, wanted, hints, prefer=None, word=False):
    """Pure target matching. pairs: list of (workset id int, name).
    wanted may be a workset id (int) or an exact, case-insensitive name; with no wanted,
    guess by hints (substrings, or whole words when word=True). When several match, a
    single one that also matches a `prefer` substring wins (for example "2_RVT Links" over
    "3_CAD Links" for Revit links). When none or several match, the target is None so the
    skill asks the user. Returns (workset id or None, list of candidate names)."""
    if isinstance(wanted, int) and not isinstance(wanted, bool):
        for wid, name in pairs:
            if wid == wanted:
                return wid, [name]
        return None, []
    if wanted:
        for wid, name in pairs:
            if name.lower() == wanted.lower():
                return wid, [name]
        return None, []
    cands = [(wid, name) for wid, name in pairs if any(name_has(name, h, word) for h in hints)]
    if len(cands) == 1:
        return cands[0][0], [cands[0][1]]
    if len(cands) > 1 and prefer:
        best = [c for c in cands if any(name_has(c[1], p) for p in prefer)]
        if len(best) == 1:
            return best[0][0], [c[1] for c in cands]
    return None, [c[1] for c in cands]


def split_cad(items, target_id):
    """Pure decision for the cad_links rule. items: list of (element id, workset id,
    view_specific). Returns (off_target ids, skipped list). A view-specific element is
    always skipped with the reason "view workset", because its workset follows its view.
    With no target (target_id None), every other element is off target, so the audit
    lists it with "no target workset chosen"."""
    off, skipped = [], []
    for eid, ws_id, view_specific in items:
        if view_specific:
            skipped.append({"element_id": eid, "reason": "view workset"})
        elif target_id is None or ws_id != target_id:
            off.append(eid)
    return off, skipped


def find_workset(worksets, wanted, hints, prefer=None, word=False):
    """match_workset on Workset objects. Returns (workset or None, list of candidate names)."""
    pairs = [(eid_int(ws.Id), ws.Name) for ws in worksets]
    wid, cands = match_workset(pairs, wanted, hints, prefer, word)
    for ws in worksets:
        if wid is not None and eid_int(ws.Id) == wid:
            return ws, cands
    return None, cands


def movable(el):
    """(True, "") when the element workset can be changed, else (False, reason)."""
    p = el.get_Parameter(DB.BuiltInParameter.ELEM_PARTITION_PARAM)
    if p is None or p.IsReadOnly:
        return False, "workset is read-only (follows its host or is fixed)"
    if el.GroupId != DB.ElementId.InvalidElementId:
        return False, "element is inside a group"
    status = DB.WorksharingUtils.GetCheckoutStatus(doc, el.Id)
    if status == DB.CheckoutStatus.OwnedByOtherUser:
        return False, "element is borrowed by another user"
    return True, ""


def main():
    if doc is None:
        return {"status": "no_active_document",
                "message": "Open a model in Revit, then run the audit again."}
    if doc.IsFamilyDocument:
        return {"status": "family_document",
                "message": "The active document is a family. Worksets apply only to project models."}
    if not doc.IsWorkshared:
        return {"status": "not_workshared", "document": doc.Title,
                "message": "This model is not workshared. It has no worksets to audit."}

    rules = inputs.get("rules") or ["links", "cad_links", "levels_grids", "workset1"]
    max_items = int(inputs.get("max_items") or 500)
    default_name = inputs.get("default_workset") or "Workset1"

    worksets = list(DB.FilteredWorksetCollector(doc).OfKind(DB.WorksetKind.UserWorkset))
    ws_by_int = {}
    ws_rows = []
    notes = []
    closed = []
    for ws in worksets:
        ws_by_int[eid_int(ws.Id)] = ws
        count = None  # elements on a closed workset are not loaded, so they cannot be counted
        if ws.IsOpen:
            count = (DB.FilteredElementCollector(doc)
                     .WherePasses(DB.ElementWorksetFilter(ws.Id, False))
                     .WhereElementIsNotElementType()
                     .GetElementCount())
        else:
            closed.append(ws.Name)
        ws_rows.append({"workset_id": eid_int(ws.Id), "name": ws.Name,
                        "element_count": count, "is_open": ws.IsOpen,
                        "is_editable": ws.IsEditable, "owner": ws.Owner or ""})
    ws_rows.sort(key=lambda r: -(r["element_count"] or 0))
    if closed:
        notes.append("Closed worksets (elements not checked): %s. Open all worksets for a full audit."
                     % ", ".join(closed))

    findings = {}
    targets = {}

    def warn_closed(rule, target):
        if target is not None and not target.IsOpen:
            notes.append("%s: target workset %s is closed. Moved elements will not show in views "
                         "until it is opened." % (rule, target.Name))

    def add(el, rule, target):
        key = eid_int(el.Id)
        if key in findings:
            return
        cur = ws_by_int.get(eid_int(el.WorksetId))
        ok, why = movable(el)
        cat = el.Category.Name if el.Category else ""
        findings[key] = {
            "element_id": key, "category": cat, "name": el.Name or "", "rule": rule,
            "from_workset": cur.Name if cur else "(not a user workset)",
            "from_workset_id": eid_int(el.WorksetId),
            "to_workset": target.Name if target else "",
            "to_workset_id": eid_int(target.Id) if target else None,
            "movable": ok and target is not None,
            "reason": why if not ok else ("" if target else "no target workset chosen"),
        }

    def pick_target(rule, wanted, hints, prefer=None, word=False):
        target, cands = find_workset(worksets, wanted, hints, prefer, word)
        targets[rule] = {"target": target.Name if target else None,
                         "target_id": eid_int(target.Id) if target else None,
                         "candidates": cands}
        if target is None:
            notes.append("%s: no single target workset found. Candidates: %s. Pass the exact name."
                         % (rule, ", ".join(cands) if cands else "none"))
        warn_closed(rule, target)
        return target

    def rule_by_class(rule, cls_list, wanted, hints, prefer=None):
        target = pick_target(rule, wanted, hints, prefer)
        for cls in cls_list:
            for el in DB.FilteredElementCollector(doc).OfClass(cls):
                if target is None or el.WorksetId != target.Id:
                    add(el, rule, target)

    if "links" in rules:
        rule_by_class("links", [DB.RevitLinkInstance], inputs.get("links_workset"), ["link"], ["rvt", "revit"])
    cad_skipped = None
    if "cad_links" in rules:
        target = pick_target("cad_links", inputs.get("cad_links_workset"), ["cad"], ["link"], word=True)
        cad_els = {}
        rows = []
        for el in DB.FilteredElementCollector(doc).OfClass(DB.ImportInstance):
            key = eid_int(el.Id)
            cad_els[key] = el
            rows.append((key, eid_int(el.WorksetId), bool(el.ViewSpecific)))
        off, cad_skipped = split_cad(rows, eid_int(target.Id) if target else None)
        for key in off:
            add(cad_els[key], "cad_links", target)
        for item in cad_skipped:
            el = cad_els[item["element_id"]]
            cur = ws_by_int.get(eid_int(el.WorksetId))
            item["name"] = el.Name or ""
            item["linked"] = bool(el.IsLinked)
            item["workset"] = cur.Name if cur else "(view workset)"
            view = doc.GetElement(el.OwnerViewId)
            item["view"] = view.Name if view is not None else ""

    if "levels_grids" in rules:
        rule_by_class("levels_grids", [DB.Level, DB.Grid],
                      inputs.get("levels_grids_workset"), ["level", "grid"])

    if "categories" in rules:
        cmap = inputs.get("category_map") or {}
        if not cmap:
            notes.append("categories: category_map is empty, so this rule found nothing.")
        for bic_name, ws_name in cmap.items():
            bic = getattr(DB.BuiltInCategory, bic_name, None)
            target, _ = find_workset(worksets, ws_name, [])
            if bic is None or target is None:
                notes.append("categories: skipped %s -> %s (unknown category or workset)."
                             % (bic_name, ws_name))
                continue
            warn_closed("categories", target)
            col = (DB.FilteredElementCollector(doc).OfCategory(bic)
                   .WhereElementIsNotElementType())
            for el in col:
                if el.WorksetId != target.Id:
                    add(el, "categories", target)

    workset1 = None
    if "workset1" in rules:
        default_ws, _ = find_workset(worksets, default_name, [])
        if default_ws is None:
            workset1 = {"workset": default_name, "found": False}
        else:
            by_cat = {}
            col = (DB.FilteredElementCollector(doc)
                   .WherePasses(DB.ElementWorksetFilter(default_ws.Id, False))
                   .WhereElementIsNotElementType())
            for el in col:
                cat = el.Category
                if cat is None or cat.CategoryType != DB.CategoryType.Model:
                    continue
                by_cat[cat.Name] = by_cat.get(cat.Name, 0) + 1
            workset1 = {"workset": default_ws.Name, "found": True,
                        "model_elements": sum(by_cat.values()),
                        "by_category": sorted(by_cat.items(), key=lambda kv: -kv[1]),
                        "note": "Report only. Give a category_map and the 'categories' rule to move them."}

    items = sorted(findings.values(), key=lambda f: (f["rule"], f["category"], f["element_id"]))
    moves = [{"element_id": f["element_id"], "from_workset_id": f["from_workset_id"],
              "to_workset_id": f["to_workset_id"]} for f in items if f["movable"]]
    return {
        "status": "ok", "document": doc.Title, "is_workshared": True,
        "worksets": ws_rows, "rules": rules, "targets": targets,
        "finding_count": len(items), "movable_count": len(moves),
        "not_movable_count": len(items) - len(moves),
        "findings": items[:max_items], "findings_truncated": len(items) > max_items,
        "moves": moves, "workset1": workset1, "notes": notes,
        "cad_links_skipped": cad_skipped,
        "cad_links_skipped_count": len(cad_skipped) if cad_skipped is not None else None,
    }

def ascii_safe(obj):
    r"""IronPython's json encoder in the agent runtime fails on non-ASCII text (for example
    "Café"). Escape every non-ASCII character as a literal \uXXXX sequence. Decode
    these sequences before you show a name to the user or write it to a CSV."""
    if isinstance(obj, dict):
        return dict((ascii_safe(k), ascii_safe(v)) for k, v in obj.items())
    if isinstance(obj, (list, tuple)):
        return [ascii_safe(x) for x in obj]
    try:
        is_text = isinstance(obj, basestring)
    except NameError:
        is_text = isinstance(obj, str)
    if is_text:
        return "".join(c if ord(c) < 128 else "\\u%04x" % ord(c) for c in obj)
    return obj


if DB is not None:
    result = ascii_safe(main())
    print("am-workset-audit: %s" % result.get("status"))
