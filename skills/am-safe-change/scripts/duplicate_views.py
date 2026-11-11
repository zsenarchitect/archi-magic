# -*- coding: utf-8 -*-
"""Read the inputs to a batch view duplication preview through Revit."""
import hashlib
import json

from System import Int64
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

import view_duplicate_preview as preview

MAX_VIEWPORTS = 20000
MAX_DETAILS = 2000


def key(identifier):
    return str(identifier.Value)


def identifier(value):
    return DB.ElementId(Int64(int(preview.element_id(value))))


def modes():
    return {"plain": DB.ViewDuplicateOption.Duplicate,
            "detailing": DB.ViewDuplicateOption.WithDetailing,
            "dependent": DB.ViewDuplicateOption.AsDependent}


def document_identity(document):
    if document is None or document.IsFamilyDocument or document.IsReadOnly or not document.PathName:
        raise ValueError("Open a saved, writable project first.")
    return {"project_uid": str(document.ProjectInformation.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}


def bounded(items, limit, label, clock):
    for index, item in enumerate(items):
        if index >= limit:
            raise ValueError("The %s inventory exceeds %d elements." % (label, limit))
        if clock.ElapsedMilliseconds > 10000:
            raise ValueError("The view snapshot exceeded ten seconds. Narrow the batch.")
        yield item


def read_view(document, view_id, template=False):
    view = document.GetElement(identifier(view_id))
    if not isinstance(view, DB.View) or view.IsTemplate != template:
        raise ValueError("Revit element %s is not the expected view or template." % view_id)
    return view


def parameter_value(parameter):
    storage = str(parameter.StorageType)
    if not parameter.HasValue:
        value = None
    elif storage == "Integer":
        value = parameter.AsInteger()
    elif storage == "Double":
        value = parameter.AsDouble()
    elif storage == "String":
        value = parameter.AsString()
    elif storage == "ElementId":
        value = key(parameter.AsElementId())
    else:
        raise ValueError("A controlled setting has an unsupported storage type.")
    return [storage, value]


def parameter_map(element):
    return {key(parameter.Id): parameter for parameter in element.Parameters}


def template_state(template):
    properties = {
        "VIEW_SCALE": ("Scale", "Scale"),
        "VIEW_SCALE_PULLDOWN_IMPERIAL": ("Scale (imperial)", "Scale"),
        "VIEW_SCALE_PULLDOWN_METRIC": ("Scale (metric)", "Scale"),
        "VIEW_DETAIL_LEVEL": ("Detail level", "DetailLevel"),
        "VIEW_PARTS_VISIBILITY": ("Parts visibility", "PartsVisibility"),
        "VIEW_DISCIPLINE": ("Discipline", "Discipline"),
        "MODEL_GRAPHICS_STYLE_ANON_DRAFT": ("Visual style", "DisplayStyle"),
    }
    scalar = ("VIEW_MODEL_DISPLAY_MODE", "VIEWER_BOUND_FAR_CLIPPING", "VIEW_SHOW_HIDDEN_LINES",
              "COLOR_SCHEME_LOCATION", "PLAN_VIEW_NORTH", "VIEW_BACK_CLIPPING")
    property_ids = {str(int(getattr(DB.BuiltInParameter, name))): fields for name, fields in properties.items()}
    scalar_ids = {str(int(getattr(DB.BuiltInParameter, name))) for name in scalar}
    excluded = {key(item) for item in template.GetNonControlledTemplateParameterIds()}
    controlled = sorted({key(item) for item in template.GetTemplateParameterIds()} - excluded, key=int)
    parameters = parameter_map(template)
    settings = []
    for parameter_id in controlled:
        if parameter_id in property_ids:
            name, attribute = property_ids[parameter_id]
            value = getattr(template, attribute)
            settings.append([parameter_id, name, ["Property", str(value)]])
        elif parameter_id in scalar_ids and parameter_id in parameters:
            parameter = parameters[parameter_id]
            settings.append([parameter_id, parameter.Definition.Name, parameter_value(parameter)])
        else:
            raise ValueError("Template %s controls a setting whose live value cannot be verified (%s)."
                             % (key(template.Id), parameter_id))
    return {"id": key(template.Id), "uid": str(template.UniqueId), "name": template.Name,
            "version": str(template.VersionGuid), "settings": settings,
            "controlled_settings": [row[1] for row in settings]}


def view_identity(view):
    return {"uid": str(view.UniqueId), "name": view.Name, "view_type": str(view.ViewType),
            "version": str(view.VersionGuid), "primary_id": key(view.GetPrimaryViewId()),
            "template_id": key(view.ViewTemplateId)}


def xyz(point):
    return [float(point.X), float(point.Y), float(point.Z)]


def detail_line(document, element):
    if not isinstance(element, DB.DetailLine) or element.GroupId.Value != -1:
        return None
    curve = element.GeometryCurve
    if not isinstance(curve, DB.Line) or not curve.IsBound:
        return None
    style = element.LineStyle
    if style is None or style.GraphicsStyleCategory is None:
        return None
    if (element.GetDependentElements(None).Count or element.GetEntitySchemaGuids().Count
            or any(element.GetAdjoinedCurveElements(end).Count for end in (0, 1))):
        return None
    category = style.GraphicsStyleCategory
    style_color = category.LineColor
    pattern_id = category.GetLinePatternId(DB.GraphicsStyleType.Projection)
    segments = []
    if pattern_id.Value > 0:
        pattern = document.GetElement(pattern_id)
        if not isinstance(pattern, DB.LinePatternElement):
            return None
        segments = [[str(segment.Type), float(segment.Length)]
                    for segment in pattern.GetLinePattern().GetSegments()]
    parameters = parameter_map(element)
    return {"id": key(element.Id), "uid": str(element.UniqueId), "version": str(element.VersionGuid),
            "type": key(element.GetTypeId()),
            "style": [key(style.Id), str(style.UniqueId), category.Name,
                      [style_color.Red, style_color.Green, style_color.Blue],
                      category.GetLineWeight(DB.GraphicsStyleType.Projection),
                      key(pattern_id), segments],
            "pinned": bool(element.Pinned),
            "points": [xyz(curve.GetEndPoint(index)) for index in (0, 1)],
            "parameters": [[item, parameter_value(parameters[item])] for item in sorted(parameters, key=int)]}


def view_details(document, view, budget, clock):
    owned = DB.FilteredElementCollector(document).OwnedByView(view.Id).WhereElementIsNotElementType()
    rows = []
    for element in bounded(owned, budget, "source annotation", clock):
        row = detail_line(document, element)
        if row is None:
            return None, len(rows) + 1
        rows.append(row)
    return sorted(rows, key=lambda row: int(row["id"])), len(rows)


def owner_state(document, view):
    if not document.IsWorkshared:
        return None
    return [str(DB.WorksharingUtils.GetCheckoutStatus(document, view.Id)),
            str(DB.WorksharingUtils.GetModelUpdatesStatus(document, view.Id))]


def can_edit(state):
    return state is None or (state[0] in ("OwnedByCurrentUser", "NotOwned")
                             and state[1] in ("CurrentWithCentral", "NotYetInCentral"))


def viewports(document, family_ids, clock):
    placements = {item: [] for item in family_ids}
    if not placements:
        return placements
    collector = DB.FilteredElementCollector(document).OfClass(DB.Viewport)
    for viewport in bounded(collector, MAX_VIEWPORTS, "viewport", clock):
        view_id = key(viewport.ViewId)
        if view_id in placements:
            sheet = document.GetElement(viewport.SheetId)
            if not isinstance(sheet, DB.ViewSheet):
                raise ValueError("An affected viewport has no verifiable sheet.")
            placements[view_id].append([key(sheet.Id), str(sheet.UniqueId), sheet.SheetNumber,
                                        key(viewport.Id), str(viewport.UniqueId)])
    return {view_id: sorted(rows) for view_id, rows in placements.items()}


def read_snapshot(document, request):
    clock = Stopwatch.StartNew()
    identity = document_identity(document)
    requested = request["sources"]
    if not isinstance(requested, list) or not 1 <= len(requested) <= preview.MAX_SOURCES:
        raise ValueError("Choose one to 50 views to duplicate.")
    native_modes = modes()
    template = read_view(document, request["template_id"], template=True)
    states = {key(template.Id): template_state(template)}
    names = sorted(view.Name for view in bounded(DB.FilteredElementCollector(document).OfClass(DB.View),
                                                 preview.MAX_VIEWS, "view", clock))
    requested_modes = {}
    for row in requested:
        source_id = preview.element_id(row["id"])
        mode = row["mode"]
        if mode not in native_modes:
            raise ValueError("Choose plain, detailing, or dependent duplication.")
        requested_modes.setdefault(source_id, set()).add(mode)

    for index, row in enumerate(requested, 1):
        view = read_view(document, row["id"])
        name = preview.duplicate_name(request["pattern"], view.Name, index, row["id"])
        if not DB.NamingUtils.IsValidName(name):
            raise ValueError("Generated name %d contains prohibited Revit characters." % index)

    sources = {}
    family_views = {}
    total_members = 0
    detail_budget = MAX_DETAILS
    for source_id, requested_options in requested_modes.items():
        view = read_view(document, source_id)
        row = view_identity(view)
        row["allowed_modes"] = sorted(option for option in requested_options
                                      if view.CanViewBeDuplicated(native_modes[option]))
        row["template_allowed"] = bool(view.IsValidViewTemplate(template.Id))
        if "detailing" in requested_options:
            details, count = view_details(document, view, detail_budget, clock)
            detail_budget -= count
            row.update(details=details, details_complete=details is not None)
        sources[source_id] = row
        if "dependent" not in requested_options:
            continue
        primary_id = row["primary_id"] if row["primary_id"] != "-1" else source_id
        if primary_id not in family_views:
            primary = read_view(document, primary_id)
            remaining = preview.MAX_FAMILY - total_members - 1
            if remaining < 0:
                raise ValueError("The batch exceeds 100 existing dependent family views.")
            member_ids = [key(item) for item in bounded(primary.GetDependentViewIds(), remaining,
                                                       "dependent family", clock)]
            family_views[primary_id] = [primary] + [read_view(document, item) for item in member_ids]
            total_members += len(member_ids) + 1

    placements = viewports(document, [key(view.Id) for views in family_views.values() for view in views], clock)
    families = {}
    for primary_id, views in family_views.items():
        members = {}
        for view in views:
            row = view_identity(view)
            old_id = row["template_id"]
            if old_id != "-1" and old_id not in states:
                states[old_id] = template_state(read_view(document, old_id, template=True))
            old = states.get(old_id, {})
            located = placements[key(view.Id)]
            if bool(located) != (str(view.GetPlacementOnSheetStatus()) != "NotPlaced"):
                raise ValueError("An affected view's sheet placement could not be verified.")
            ownership = owner_state(document, view)
            row.update(template_name=old.get("name"), controlled_settings=old.get("controlled_settings", []),
                       placement_ids=located, sheets=[item[2] for item in located],
                       ownership=ownership, writable=can_edit(ownership))
            members[key(view.Id)] = row
        families[primary_id] = {"members": members,
                                "template_allowed": bool(views[0].IsValidViewTemplate(template.Id))}
    if clock.ElapsedMilliseconds > 10000:
        raise ValueError("The view snapshot exceeded ten seconds. Narrow the batch.")
    snapshot = {"kind": "view-duplicate-snapshot", "complete": True, "document": identity,
                "request": request, "names": names, "template": states[key(template.Id)], "sources": sources,
                "families": families, "template_states": states}
    preview.snapshot_digest(snapshot)
    return snapshot


def main(document, args):
    if args.get("mode", "read") != "read":
        raise ValueError("Choose read to inspect the view duplication inputs.")
    snapshot = read_snapshot(document, args["request"])
    return {"status": "ok", "snapshot_json": json.dumps(snapshot, ensure_ascii=True, sort_keys=True,
                                                        separators=(",", ":"), allow_nan=False)}


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception:
        result = {"status": "stopped", "message": "Revit could not verify the view duplication inputs."}
