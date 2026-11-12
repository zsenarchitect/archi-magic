# -*- coding: utf-8 -*-
"""Read door and window types with instances in the active Revit project."""

import hashlib
import json
import math

from pyrevit.api import DB
from System.Diagnostics import Stopwatch

MAX_PLACEMENTS = 20000
MAX_TYPES = 500
MAX_MS = 10000
MAX_JSON = 2 * 1024 * 1024


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


def project_snapshot(document):
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
    if clock.ElapsedMilliseconds >= MAX_MS:
        raise ValueError("The placed opening inventory exceeded its limits.")
    rows = sorted(types.values(), key=lambda row: (row["category"], int(row["id"])))
    return {"kind": "placed-opening-type-snapshot", "scope": "host-project",
            "host_identity": {"project_uid": str(document.ProjectInformation.UniqueId),
                              "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()},
            "placements": totals, "types": rows}


if "doc" in globals():
    try:
        snapshot = project_snapshot(doc)
        encoded = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) > MAX_JSON:
            raise ValueError("The placed opening type snapshot exceeds its size limit.")
        result = {"status": "ok", "snapshot_json": encoded}
    except ValueError as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not complete the opening inventory.",
                  "error_type": type(error).__name__}
