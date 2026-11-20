# -*- coding: utf-8 -*-
"""READ step for detail markers (issue #257, comments of 2026-11-18). Read-only (run_query).

Lists every view with its sheet placement, and every reference marker
(reference section, callout or elevation: a view-specific marker that
points to another view) with its host view and its target view. The
terminal step (detail_marker_plan.py) uses it for:
- the duplicate-by-condition check (title on sheet, floor types),
- the audit of markers whose target view is on no sheet,
- a plan to point a marker to another view, or to remove a detail view.

inputs (all optional):
  {"sheet_id": <id>}           read the floor types that each section,
                               detail or drafting view on this sheet
                               shows (elements visible in the view).
  {"dependents_of": [<id>]}    list the elements that Revit deletes with
                               each of these views (GetDependentElements).
  {"cuts": [{"name", "start": [x, y], "end": [x, y], "z_min", "z_max",
             "padding"}]}      floor types whose bounding box meets a thin
                               box along each requested cut (feet). This
                               is a candidate list, not a fact.

A viewport with no sheet (SheetId -1) is a hidden helper of a section
view, not a placement. It is listed under "hidden_viewport_ids".

result = {"document", "escaped",
          "views": [{"id", "name", "view_type", "title_on_sheet",
                     "sheet": null | {"sheet_id", "sheet_number",
                                      "viewport_id", "detail_number"},
                     "hidden_viewport_ids": [...],
                     "floor_types": [...]  (only with sheet_id)}],
          "references": [{"id", "kind", "name", "host_view_id",
                          "target_view_id"}],
          "dependents": {"<view id>": [{"id", "category", "class", "name",
                                        "owner_view_id"}]},
          "cuts": [{"name", "floor_types": [...]}]}
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
import sys

from pyrevit.api import DB

ESCAPE = sys.platform == "cli"  # IronPython result encoder is ASCII-only (see read_sheets.py)
MAX_VIEWS = 5000      # the query budget in am-revit-api-docs
MAX_DEPENDENTS = 5000
MAX_FLOOR_VIEWS = 200
SKIP_TYPES = ("DrawingSheet", "Schedule", "PanelSchedule", "ColumnSchedule", "Legend",
              "Report", "SystemBrowser", "ProjectBrowser", "Undefined", "Internal")


def txt(s):
    if s is None or not ESCAPE:
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


def id_int(element_id):
    try:
        return int(element_id.Value)        # Revit 2024+
    except AttributeError:
        return int(element_id.IntegerValue)  # removed in Revit 2026


# DB.ElementId(int) is ambiguous in IronPython (issue #50).
try:
    import System

    def eid(n):
        return DB.ElementId(System.Int64(int(n)))
except ImportError:
    def eid(n):
        return DB.ElementId(int(n))


def param_text(element, bip):
    try:
        p = element.get_Parameter(bip)
    except Exception:
        return None
    if p is None:
        return None
    try:
        return p.AsString() or p.AsValueString()
    except Exception:
        return None


def element_name(element):
    try:
        return element.Name
    except Exception:
        pass
    try:
        return DB.Element.Name.GetValue(element)
    except Exception:
        return ""


def type_name(element):
    t = doc.GetElement(element.GetTypeId())  # noqa: F821
    if t is None:
        return None
    name = param_text(t, DB.BuiltInParameter.ALL_MODEL_TYPE_NAME)
    return name or element_name(t)


def detail_number(vp):
    text = param_text(vp, DB.BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
    if text is None:
        try:
            p = vp.LookupParameter("Detail Number")
            text = p.AsString() if p is not None else None
        except Exception:
            text = None
    return txt(text)


def int_list(value):
    out = []
    for v in value or []:
        try:
            out.append(int(v))
        except Exception:
            pass
    return out


want_sheet = None
try:
    want_sheet = int(inputs.get("sheet_id"))  # noqa: F821
except Exception:
    want_sheet = None
dependents_of = int_list(inputs.get("dependents_of"))  # noqa: F821
cuts_in = inputs.get("cuts") or []  # noqa: F821

# Sheet placements. A viewport on no sheet (SheetId -1) is a hidden helper.
placed = {}
hidden = {}
for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport):  # noqa: F821
    view_key = id_int(vp.ViewId)
    sheet = doc.GetElement(vp.SheetId) if id_int(vp.SheetId) > 0 else None  # noqa: F821
    if not isinstance(sheet, DB.ViewSheet):
        hidden.setdefault(view_key, []).append(id_int(vp.Id))
        continue
    placed[view_key] = {"sheet_id": id_int(sheet.Id), "sheet_number": txt(sheet.SheetNumber),
                        "viewport_id": id_int(vp.Id), "detail_number": detail_number(vp)}

views = []
host_views = []
floor_views = 0
for view in DB.FilteredElementCollector(doc).OfClass(DB.View):  # noqa: F821
    if view.IsTemplate:
        continue
    vtype = str(view.ViewType)
    if vtype in SKIP_TYPES:
        continue
    if len(views) >= MAX_VIEWS:
        raise ValueError("More than %d views. Narrow the scope first." % MAX_VIEWS)
    key = id_int(view.Id)
    row = {"id": key, "name": txt(view.Name), "view_type": vtype,
           "title_on_sheet": txt(param_text(view, DB.BuiltInParameter.VIEW_DESCRIPTION)),
           "sheet": placed.get(key), "hidden_viewport_ids": hidden.get(key, [])}
    sheet = placed.get(key)
    if (want_sheet is not None and sheet is not None and sheet["sheet_id"] == want_sheet
            and vtype in ("Section", "Detail", "DraftingView")):
        floor_views += 1
        if floor_views > MAX_FLOOR_VIEWS:
            raise ValueError("More than %d views on the sheet. Narrow the scope first." % MAX_FLOOR_VIEWS)
        names = set()
        collector = (DB.FilteredElementCollector(doc, view.Id)  # noqa: F821
                     .OfCategory(DB.BuiltInCategory.OST_Floors).WhereElementIsNotElementType())
        for floor in collector:
            name = type_name(floor)
            if name:
                names.add(txt(name))
        row["floor_types"] = sorted(names)
    views.append(row)
    host_views.append(view)

references = []
for view in host_views:
    for kind, getter in (("section", "GetReferenceSections"), ("callout", "GetReferenceCallouts"),
                         ("elevation", "GetReferenceElevations")):
        try:
            ids = list(getattr(view, getter)())
        except Exception:
            continue
        for rid in ids:
            try:
                target = id_int(DB.ReferenceableViewUtils.GetReferencedViewId(doc, rid))  # noqa: F821
            except Exception:
                target = None
            element = doc.GetElement(rid)  # noqa: F821
            references.append({"id": id_int(rid), "kind": kind,
                               "name": txt(element_name(element)) if element is not None else None,
                               "host_view_id": id_int(view.Id),
                               "target_view_id": target if target and target > 0 else None})

dependents = {}
for view_key in dependents_of:
    view = doc.GetElement(eid(view_key))  # noqa: F821
    if view is None or not isinstance(view, DB.View):
        dependents[str(view_key)] = None
        continue
    rows = []
    for dep_id in view.GetDependentElements(None):
        if len(rows) >= MAX_DEPENDENTS:
            raise ValueError("View %s has more than %d dependent elements." % (view_key, MAX_DEPENDENTS))
        element = doc.GetElement(dep_id)  # noqa: F821
        if element is None:
            continue
        category = element.Category.Name if element.Category is not None else None
        owner = None
        try:
            owner = id_int(element.OwnerViewId)
        except Exception:
            owner = None
        rows.append({"id": id_int(dep_id), "category": txt(category),
                     "class": element.GetType().Name, "name": txt(element_name(element)),
                     "owner_view_id": owner if owner and owner > 0 else None})
    dependents[str(view_key)] = rows

cuts = []
for cut in cuts_in:
    pad = float(cut.get("padding") or 0.5)
    xs = [float(cut["start"][0]), float(cut["end"][0])]
    ys = [float(cut["start"][1]), float(cut["end"][1])]
    low = DB.XYZ(min(xs) - pad, min(ys) - pad, float(cut.get("z_min", -1.0)))
    high = DB.XYZ(max(xs) + pad, max(ys) + pad, float(cut.get("z_max", 1.0)))
    names = set()
    collector = (DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Floors)  # noqa: F821
                 .WhereElementIsNotElementType()
                 .WherePasses(DB.BoundingBoxIntersectsFilter(DB.Outline(low, high))))
    for floor in collector:
        name = type_name(floor)
        if name:
            names.add(txt(name))
    cuts.append({"name": txt(cut.get("name")), "floor_types": sorted(names)})

result = {"document": txt(doc.Title), "views": views, "references": references,  # noqa: F821
          "dependents": dependents, "cuts": cuts, "escaped": ESCAPE}
