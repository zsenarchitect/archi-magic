# -*- coding: utf-8 -*-
"""Read door and window types with instances in the active Revit project."""

import hashlib
import json
import math
import re

from pyrevit.api import DB
from System.Diagnostics import Stopwatch

MAX_PLACEMENTS = 20000
MAX_TYPES = 500
MAX_MS = 10000
MAX_JSON = 2 * 1024 * 1024
MAX_LEGEND_ELEMENTS = 1000
TEXT = (str, type(u""))


def element_id(identifier):
    try:
        return str(identifier.Value)
    except AttributeError:
        return str(identifier.IntegerValue)


def dimension(owner, parameter, include_display=True):
    value = owner.get_Parameter(parameter)
    if value is None:
        return None
    if value.StorageType != DB.StorageType.Double:
        raise ValueError("An opening size parameter has an unexpected storage type.")
    if not value.HasValue:
        return None
    raw = float(value.AsDouble())
    if raw <= 0 or math.isnan(raw) or math.isinf(raw):
        return None
    return {"feet": raw, "display": value.AsValueString() or None} if include_display else raw


def opening_type(symbol, key, label, width_param, height_param):
    if not isinstance(symbol, DB.FamilySymbol) or not symbol.UniqueId or not symbol.FamilyName or not symbol.Name:
        raise ValueError("A placed opening type could not be identified.")
    parameter = symbol.get_Parameter(DB.BuiltInParameter.ALL_MODEL_TYPE_MARK)
    if parameter is not None and parameter.StorageType != DB.StorageType.String:
        raise ValueError("A type mark has an unexpected storage type.")
    mark = parameter.AsString() if parameter is not None and parameter.HasValue else None
    return {"id": key, "uid": str(symbol.UniqueId), "category": label,
            "family": symbol.FamilyName, "type": symbol.Name, "type_mark": mark,
            "width": dimension(symbol, width_param), "height": dimension(symbol, height_param),
            "placements": 0, "instance_dimension_counts": {"width": 0, "height": 0},
            "instance_dimension_ranges_ft": {"width": None, "height": None},
            "instance_dimensions_vary": {"width": False, "height": False}}


def positive_id(value):
    from System import Int64

    if (not isinstance(value, TEXT) or not re.match(r"^[1-9][0-9]{0,18}\Z", value)
            or int(value) >= 2**63):
        raise ValueError("Select the legend by its positive decimal-string Revit ID.")
    return DB.ElementId(Int64(int(value)))


def vector(point):
    values = [float(point.X), float(point.Y), float(point.Z)]
    if any(math.isnan(value) or math.isinf(value) for value in values):
        raise ValueError("A legend coordinate is not finite.")
    return values


def element_bounds(element, view):
    box = element.get_BoundingBox(view)
    if box is None:
        return None
    try:
        return {"min": vector(box.Min), "max": vector(box.Max)}
    finally:
        box.Dispose()


def legend_component(document, element, view, opening_types, known_directions):
    parameters = DB.BuiltInParameter
    reference = element.get_Parameter(parameters.LEGEND_COMPONENT)
    direction_param = element.get_Parameter(parameters.LEGEND_COMPONENT_VIEW)
    if reference is None or reference.StorageType != DB.StorageType.ElementId or not reference.HasValue:
        raise ValueError("A legend component has no readable type reference.")
    if direction_param is None or direction_param.StorageType != DB.StorageType.Integer or not direction_param.HasValue:
        raise ValueError("A legend component has no readable view direction.")
    identifier = reference.AsElementId()
    type_id = element_id(identifier)
    symbol = document.GetElement(identifier)
    if symbol is None or not symbol.UniqueId:
        raise ValueError("A legend component references an unavailable type.")
    category_id = element_id(symbol.Category.Id) if symbol.Category is not None else None
    directions = {-7: "front_elevation", -6: "back_elevation", -8: "floor_plan", -5: "section",
                  -4: "ceiling_plan", -3: "three_dimensional"}
    value = direction_param.AsInteger()
    category = opening_types.get(category_id) if isinstance(symbol, DB.FamilySymbol) else None
    length_param = element.get_Parameter(parameters.LEGEND_COMPONENT_LENGTH)
    length = None
    if length_param is not None and length_param.StorageType == DB.StorageType.Double and length_param.HasValue:
        length = float(length_param.AsDouble())
        if math.isnan(length) or math.isinf(length):
            raise ValueError("A legend component length is not finite.")
    return {"id": element_id(element.Id), "uid": str(element.UniqueId), "type_id": type_id,
            "type_uid": str(symbol.UniqueId), "opening_category": category, "direction_raw": value,
            "direction": directions.get(value) if known_directions else None, "host_length_ft": length,
            "pinned": bool(element.Pinned), "type_read_only": bool(reference.IsReadOnly),
            "direction_read_only": bool(direction_param.IsReadOnly),
            "bounds_ft": element_bounds(element, view)}


def coverage_report(types, components, known_directions):
    if not known_directions:
        return None
    by_type = {}
    for row in components:
        if row["opening_category"] and row["direction"] in ("front_elevation", "back_elevation"):
            by_type.setdefault(row["type_id"], []).append(row["id"])
    unresolved = {row["type_id"] for row in components if row["opening_category"] and row["direction"] is None}
    return {
        "missing_type_ids": [row["id"] for row in types if row["id"] not in by_type and row["id"] not in unresolved],
        "unresolved_type_ids": sorted(unresolved, key=int),
        "duplicate_elevation_type_ids": [key for key in sorted(by_type, key=int) if len(by_type[key]) > 1],
        "unplaced_elevation_type_ids": sorted(set(by_type) - {row["id"] for row in types}, key=int),
        "unknown_direction_component_ids": [row["id"] for row in components
                                            if row["opening_category"] and row["direction"] is None],
    }


def legend_snapshot(document, identifier, types, clock):
    view = document.GetElement(positive_id(identifier))
    if not isinstance(view, DB.View) or view.IsTemplate or view.ViewType != DB.ViewType.Legend or not view.UniqueId:
        raise ValueError("Select a real legend view in the current Revit project.")
    year = str(document.Application.VersionNumber)
    known_directions = year == "2026"
    categories = DB.BuiltInCategory
    opening_types = {str(int(categories.OST_Doors)): "Doors", str(int(categories.OST_Windows)): "Windows"}
    components, notes = [], []
    for category in (categories.OST_LegendComponents, categories.OST_TextNotes):
        collector = DB.FilteredElementCollector(document)
        try:
            for element in collector.OwnedByView(view.Id).OfCategory(category).WhereElementIsNotElementType():
                if len(components) + len(notes) >= MAX_LEGEND_ELEMENTS or clock.ElapsedMilliseconds >= MAX_MS:
                    raise ValueError("The legend inventory exceeded its limits.")
                if element_id(element.OwnerViewId) != identifier or not element.UniqueId:
                    raise ValueError("A legend element does not belong to the selected view.")
                if category == categories.OST_LegendComponents:
                    components.append(legend_component(document, element, view, opening_types, known_directions))
                else:
                    notes.append({"id": element_id(element.Id), "uid": str(element.UniqueId), "text": element.Text,
                                  "type_id": element_id(element.GetTypeId()), "position_ft": vector(element.Coord),
                                  "pinned": bool(element.Pinned), "bounds_ft": element_bounds(element, view)})
        finally:
            collector.Dispose()
    components.sort(key=lambda row: int(row["id"]))
    notes.sort(key=lambda row: int(row["id"]))
    return {"id": identifier, "uid": str(view.UniqueId), "name": view.Name, "scale": int(view.Scale),
            "revit_year": year, "directions_verified_for_year": known_directions,
            "origin_ft": vector(view.Origin), "right": vector(view.RightDirection), "up": vector(view.UpDirection),
            "components": components, "text_notes": notes,
            "coverage": coverage_report(types, components, known_directions)}


def project_snapshot(document, request=None):
    if (document is None or document.IsFamilyDocument or document.IsLinked or not document.PathName
            or document.ProjectInformation is None or not document.ProjectInformation.UniqueId):
        raise ValueError("Open a saved host Revit project before reading placed opening types.")
    clock = Stopwatch.StartNew()
    totals = {"Doors": 0, "Windows": 0}
    parameters = DB.BuiltInParameter
    definitions = ((DB.BuiltInCategory.OST_Doors, "Doors", parameters.DOOR_WIDTH, parameters.DOOR_HEIGHT),
                   (DB.BuiltInCategory.OST_Windows, "Windows", parameters.WINDOW_WIDTH, parameters.WINDOW_HEIGHT))
    types = {}
    count = 0
    for category, label, width_param, height_param in definitions:
        collector = DB.FilteredElementCollector(document)
        try:
            for instance in collector.OfCategory(category).OfClass(DB.FamilyInstance).WhereElementIsNotElementType():
                totals[label] += 1
                count += 1
                if count > MAX_PLACEMENTS or clock.ElapsedMilliseconds >= MAX_MS:
                    raise ValueError("The placed opening inventory exceeded its limits.")
                identifier = instance.GetTypeId()
                key = element_id(identifier)
                if int(key) <= 0:
                    raise ValueError("A placed opening has no valid type ID.")
                row = types.get(key)
                if row is None:
                    if len(types) >= MAX_TYPES:
                        raise ValueError("The placed opening type count exceeded its limit.")
                    symbol = document.GetElement(identifier)
                    row = opening_type(symbol, key, label, width_param, height_param)
                    types[key] = row
                if row["category"] != label:
                    raise ValueError("A placed opening type appears in more than one category.")
                row["placements"] += 1
                for field, param in (("width", width_param), ("height", height_param)):
                    raw = dimension(instance, param, include_display=False)
                    if raw is None:
                        continue
                    row["instance_dimension_counts"][field] += 1
                    ranges = row["instance_dimension_ranges_ft"]
                    low, high = ranges[field] or [raw, raw]
                    ranges[field] = [min(low, raw), max(high, raw)]
                    nominal = row[field]
                    if abs(raw - low) > 1e-7 or abs(raw - high) > 1e-7 or (
                            nominal and abs(raw - nominal["feet"]) > 1e-7):
                        row["instance_dimensions_vary"][field] = True
        finally:
            collector.Dispose()
    rows = sorted(types.values(), key=lambda row: (row["category"], int(row["id"])))
    target = legend_snapshot(document, request["legend_id"], rows, clock) if request and "legend_id" in request else None
    if clock.ElapsedMilliseconds >= MAX_MS:
        raise ValueError("The opening and legend inventory exceeded its limits.")
    result = {"kind": "placed-opening-type-snapshot", "scope": "host-project",
            "host_identity": {"project_uid": str(document.ProjectInformation.UniqueId),
                              "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()},
            "placements": totals, "types": rows}
    if target is not None:
        result["legend"] = target
    return result


if "doc" in globals():
    try:
        snapshot = project_snapshot(doc, globals().get("inputs"))
        encoded = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) > MAX_JSON:
            raise ValueError("The placed opening type snapshot exceeds its size limit.")
        result = {"status": "ok", "snapshot_json": encoded}
    except ValueError as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not complete the opening inventory.",
                  "error_type": type(error).__name__}
