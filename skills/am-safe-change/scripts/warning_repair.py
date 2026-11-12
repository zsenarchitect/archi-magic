# -*- coding: utf-8 -*-
"""Read native warning repair evidence through the pyRevit Agent Runtime."""
import hashlib
import json
import math

from System import Int64
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

import warning_repair_preview as preview


def key(identifier):
    return str(identifier.Value)


def identifier(value):
    return DB.ElementId(Int64(int(preview.element_id(value))))


def project_identity(document):
    if document is None or document.IsFamilyDocument or document.IsReadOnly or not document.PathName:
        raise ValueError("Open a saved, writable Revit project.")
    project = document.ProjectInformation
    if project is None or not project.UniqueId:
        raise ValueError("The project identity is unavailable.")
    return {"project_uid": str(project.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}


def clock_check(clock):
    if clock.ElapsedMilliseconds > 10000:
        raise ValueError("The native repair read exceeded its ten-second budget.")


def native_types():
    overlap = DB.BuiltInFailures.OverlapFailures
    performance = DB.BuiltInFailures.PerformanceFailures
    return {str(overlap.DuplicateInstances.Guid): "duplicate-furniture",
            str(performance.DuplicateInstances.Guid): "duplicate-furniture",
            str(overlap.RoomSeparationLinesOverlap.Guid): "coincident-room-separators"}


def native_warnings(document, clock):
    messages = document.GetWarnings()
    clock_check(clock)
    if len(messages) > preview.MAX_WARNINGS:
        raise ValueError("More than 5,000 warnings. The warning inventory is incomplete.")
    rows = []
    references = 0
    for message in messages:
        failing = message.GetFailingElements()
        additional = message.GetAdditionalElements()
        clock_check(clock)
        references += len(failing) + len(additional)
        if references > preview.MAX_REFERENCES:
            raise ValueError("More than 20,000 warning element references.")
        rows.append([str(message.GetFailureDefinitionId().Guid),
                     [key(item) for item in failing], [key(item) for item in additional]])
    return preview.warning_rows(preview.warning_multiset(rows))


def coordinates(point):
    return tuple(float(value) for value in (point.X, point.Y, point.Z))


def option_id(element):
    return key(element.DesignOption.Id) if element.DesignOption is not None else "-1"


def ownership(document, element):
    if not document.IsWorkshared:
        return None
    status = str(DB.WorksharingUtils.GetCheckoutStatus(document, element.Id))
    updates = str(DB.WorksharingUtils.GetModelUpdatesStatus(document, element.Id))
    return [status, updates]


def writable(state):
    return state is None or (state[0] in ("NotOwned", "OwnedByCurrentUser")
                             and state[1] in ("CurrentWithCentral", "NotYetInCentral"))


def typed_parameters(element, budget, clock):
    rows = []
    for parameter in element.Parameters:
        clock_check(clock)
        budget["parameters"] -= 1
        if budget["parameters"] < 0:
            raise ValueError("More than 4,000 parameter values in the repair batch.")
        storage = str(parameter.StorageType)
        if storage == "None":
            raise ValueError("An instance parameter has an unsupported storage type.")
        getter = {"Integer": "AsInteger", "Double": "AsDouble", "String": "AsString", "ElementId": "AsElementId"}.get(storage)
        if getter is None:
            raise ValueError("An instance parameter has an unsupported storage type.")
        value = getattr(parameter, getter)() if parameter.HasValue else None
        if storage == "ElementId" and value is not None:
            value = key(value)
        shared_guid = str(parameter.GUID) if parameter.IsShared else None
        rows.append([key(parameter.Id), shared_guid, storage, value])
    # Use the parameter identity and typed value, including repeated IDs.
    return sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=True, allow_nan=False))


def empty_relationships(element, budget, clock):
    dependents = element.GetDependentElements(None)
    entities = element.GetEntitySchemaGuids()
    clock_check(clock)
    budget["relationships"] -= len(dependents) + len(entities)
    if budget["relationships"] < 0:
        raise ValueError("More than 4,000 relationship records in the repair batch.")
    return not len(dependents) and not len(entities)


def furniture_state(document, element):
    if (not isinstance(element, DB.FamilyInstance)
            or element.Category.Id.Value != int(DB.BuiltInCategory.OST_Furniture)
            or not isinstance(element.Location, DB.LocationPoint)
            or element.Symbol is None or element.Symbol.Family.IsInPlace
            or element.Symbol.Family.FamilyPlacementType != DB.FamilyPlacementType.OneLevelBased
            or element.Host is not None or element.HostFace is not None or element.SuperComponent is not None
            or element.MEPModel is not None or len(element.GetSubComponentIds())):
        return None
    level = document.GetElement(element.LevelId)
    if not isinstance(level, DB.Level):
        return None
    transform = element.GetTransform()
    return {"level": key(level.Id), "symbol": key(element.Symbol.Id),
            "point": coordinates(element.Location.Point), "rotation": float(element.Location.Rotation),
            "transform": [coordinates(value) for value in
                          (transform.Origin, transform.BasisX, transform.BasisY, transform.BasisZ)],
            "flip_mirror": [bool(element.FacingFlipped), bool(element.HandFlipped), bool(element.Mirrored)]}


def separator_state(document, element, clock):
    if (not isinstance(element, DB.ModelLine)
            or element.Category.Id.Value != int(DB.BuiltInCategory.OST_RoomSeparationLines)
            or element.SketchPlane is None
            or any(len(element.GetAdjoinedCurveElements(end)) for end in (0, 1))):
        return None
    curve = element.GeometryCurve
    if not isinstance(curve, DB.Line) or not curve.IsBound:
        return None
    plane = element.SketchPlane.GetPlane()
    normal = coordinates(plane.Normal)
    if normal not in ((0.0, 0.0, 1.0), (0.0, 0.0, -1.0)):
        return None
    elevation = float(plane.Origin.Z)
    level = document.GetElement(element.LevelId) if element.LevelId.Value > 0 else None
    if level is None:
        matches = []
        for candidate in DB.FilteredElementCollector(document).OfClass(DB.Level):
            clock_check(clock)
            if abs(float(candidate.ProjectElevation) - elevation) <= 1e-7:
                matches.append(candidate)
                if len(matches) > 1:
                    return None
        level = matches[0] if matches else None
    if not isinstance(level, DB.Level) or abs(float(level.ProjectElevation) - elevation) > 1e-7:
        return None
    endpoints = sorted(coordinates(curve.GetEndPoint(end)) for end in (0, 1))
    if endpoints[0] == endpoints[1] or any(abs(point[2] - elevation) > 1e-7 for point in endpoints):
        return None
    return {"level": key(level.Id), "elevation": elevation, "sketch_plane": key(element.SketchPlane.Id),
            "endpoints": endpoints}


def describe_element(document, value, recipe, budget, clock):
    element = document.GetElement(identifier(value))
    clock_check(clock)
    if element is None or element.Category is None:
        return {"eligible": False}
    result = {"uid": str(element.UniqueId), "recipe": recipe, "eligible": False}
    if (element.Pinned or element.ViewSpecific or element.GroupId.Value != -1
            or element.AssemblyInstanceId.Value != -1 or not empty_relationships(element, budget, clock)):
        return result
    if recipe == "duplicate-furniture":
        shape = furniture_state(document, element)
    elif recipe == "coincident-room-separators":
        shape = separator_state(document, element, clock)
    else:
        return result
    if shape is None:
        return result
    state = ownership(document, element)
    context = {"workset": str(element.WorksetId.IntegerValue), "option": option_id(element),
               "created_phase": key(element.CreatedPhaseId), "demolished_phase": key(element.DemolishedPhaseId)}
    result.update(eligible=True, writable=writable(state), ownership=state,
                  equivalence={"shape": shape, "context": context,
                               "parameters": typed_parameters(element, budget, clock)})
    clock_check(clock)
    return result


def canonical_loop(edges):
    if len(edges) < 2:
        raise ValueError("A room boundary loop is incomplete.")
    starts = [edge[2][0] for edge in edges]
    if len(set(starts)) != len(starts):
        raise ValueError("A room boundary loop repeats a start point.")
    for index, (_, _, points) in enumerate(edges):
        start = starts[(index + 1) % len(edges)]
        if sum((a - b) ** 2 for a, b in zip(points[-1], start)) > 1e-14:
            raise ValueError("A room boundary loop is open.")

    def rotate(rows):
        index = min(range(len(rows)), key=lambda i: rows[i][2][0])
        return rows[index:] + rows[:index]

    reverse = [(source, linked, tuple(reversed(points))) for source, linked, points in reversed(edges)]
    return min(rotate(edges), rotate(reverse))


def room_inventory(document, clock, aliases=None):
    aliases = aliases or {}
    rooms = {}
    room_count = 0
    segment_count = 0
    options = DB.SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    try:
        collector = DB.FilteredElementCollector(document).OfCategory(DB.BuiltInCategory.OST_Rooms)
        for room in collector.WhereElementIsNotElementType():
            clock_check(clock)
            room_count += 1
            if room_count > 1000:
                raise ValueError("More than 1,000 rooms. The room census is incomplete.")
            if not isinstance(room, DB.Architecture.Room):
                raise ValueError("The room census found an unsupported element.")
            location = room.Location
            if location is None:
                continue
            area = float(room.Area)
            if math.isnan(area) or math.isinf(area) or area < 0:
                raise ValueError("A placed room has an invalid area.")
            loops = room.GetBoundarySegments(options)
            clock_check(clock)
            if (loops is not None and len(loops) > 0) != (area > 0):
                raise ValueError("A room's area and boundary availability disagree.")
            rows = []
            for loop in loops or []:
                segment_count += len(loop)
                if segment_count > 10000:
                    raise ValueError("More than 10,000 room boundary segments.")
                edges = []
                for segment in loop:
                    clock_check(clock)
                    source, linked = key(segment.ElementId), key(segment.LinkElementId)
                    curve = segment.GetCurve()
                    if not isinstance(curve, (DB.Line, DB.Arc)) or not curve.IsBound:
                        raise ValueError("A room has boundary geometry that cannot be verified.")
                    points = [coordinates(curve.GetEndPoint(0))]
                    if isinstance(curve, DB.Arc):
                        points.append(coordinates(curve.Evaluate(0.5, True)))
                    points.append(coordinates(curve.GetEndPoint(1)))
                    if linked == "-1":
                        source = aliases.get(source, source)
                    edges.append((source, linked, tuple(points)))
                rows.append(canonical_loop(edges))
            room_id = key(room.Id)
            if room_id in rooms or room.LevelId.Value <= 0 or not room.UniqueId:
                raise ValueError("A placed room has no verifiable identity or level.")
            rooms[room_id] = {"uid": str(room.UniqueId), "location": coordinates(location.Point),
                              "level": key(room.LevelId), "phase": key(room.CreatedPhaseId),
                              "option": option_id(room), "enclosed": bool(rows), "area": area,
                              "loops": sorted(rows)}
        clock_check(clock)
        return rooms
    finally:
        options.Dispose()


def read_snapshot(document, request):
    clock = Stopwatch.StartNew()
    identity = project_identity(document)
    allowed = native_types()
    pairs = request["pairs"]
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= preview.MAX_PAIRS:
        raise ValueError("Choose one to 20 explicit keep and remove pairs.")
    members = [preview.element_id(pair[role]) for pair in pairs for role in ("keep_id", "remove_id")]
    if len(set(members)) != len(members):
        raise ValueError("Repair pairs must be disjoint.")
    warnings = native_warnings(document, clock)
    elements = {}
    budget = {"parameters": 4000, "relationships": 4000}
    for pair in pairs:
        recipe = allowed.get(preview.failure_id(pair["failure_id"]))
        for field in ("keep_id", "remove_id"):
            item = pair[field]
            elements[item] = describe_element(document, item, recipe, budget, clock)
    needs_rooms = any(allowed.get(pair["failure_id"]) == "coincident-room-separators" for pair in pairs)
    rooms = room_inventory(document, clock) if needs_rooms else None
    snapshot = {"kind": "warning-repair-snapshot", "complete": True, "document": identity,
                "request": request, "warnings": warnings, "repair_types": allowed, "elements": elements,
                "room_inventory_complete": needs_rooms, "rooms": rooms}
    preview.snapshot_digest(snapshot)
    clock_check(clock)
    return snapshot


def main(document, args):
    if args.get("mode", "read") != "read":
        raise ValueError("Choose read for a native warning repair snapshot.")
    snapshot = read_snapshot(document, args["request"])
    return {"status": "ok", "snapshot_json": json.dumps(snapshot, ensure_ascii=True, sort_keys=True,
                                                        separators=(",", ":"), allow_nan=False)}


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not verify repair evidence (%s)."
                  % type(error).__name__}
