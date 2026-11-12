# -*- coding: utf-8 -*-
"""Inventory selected room boundaries in Revit 2026 via run_query only."""

import hashlib
import json
import math
from pyrevit.api import DB
from System import Int64
from System.Collections.Generic import List


MAX_ROOMS = 20
MAX_SEGMENTS = 2000
TOLERANCE_FT = 1e-6


def ids_from(values):
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ROOMS:
        raise ValueError("Pass 1 to 20 room IDs in inputs.room_ids.")
    ids = set()
    for raw in values:
        text = str(raw)
        if not text.isdigit():
            raise ValueError("Room IDs must be positive whole numbers.")
        value = int(text)
        if not 0 < value <= 9223372036854775807:
            raise ValueError("Room ID is outside the supported range.")
        ids.add(value)
    return sorted(ids)


def xyz(point):
    return [point.X, point.Y, point.Z]


def excluded(room_id, reason):
    return {"room_id": room_id, "status": "excluded", "reason": reason}


def finish_types(document, request):
    if not isinstance(request, dict) or not request or set(request) - {"floor", "ceiling"}:
        raise ValueError("Select a floor finish, a ceiling finish, or both.")
    selected = {}
    for kind, entry in request.items():
        if not isinstance(entry, dict) or set(entry) != {"type_id", "offset_ft"}:
            raise ValueError("Supply a finish type ID and level-relative offset in feet.")
        type_id, = ids_from([entry["type_id"]])
        element = document.GetElement(DB.ElementId(Int64(type_id)))
        expected = DB.FloorType if kind == "floor" else DB.CeilingType
        offset = entry["offset_ft"]
        if (not isinstance(element, expected) or not element.UniqueId
                or (kind == "floor" and element.IsFoundationSlab)):
            raise ValueError("Select a non-foundation floor type or an exact ceiling type.")
        if type(offset) not in (int, float) or not finite(offset):
            raise ValueError("The level-relative offset must be a finite number of feet.")
        selected[kind] = {"type_id": str(type_id), "type_uid": str(element.UniqueId), "offset_ft": offset}
    return selected


def finite(value):
    try:
        return not math.isnan(value) and not math.isinf(value)
    except OverflowError:
        return False


def room_finish_preview(document, room, row, types):
    if room.DesignOption is not None:
        return excluded(row["room_id"], "room_design_option_requires_review")
    level = document.GetElement(room.LevelId)
    if not isinstance(level, DB.Level) or not level.UniqueId or not finite(level.ProjectElevation):
        return excluded(row["room_id"], "room_level_identity_unavailable")
    parameter = room.get_Parameter(DB.BuiltInParameter.ROOM_PHASE_ID)
    phase = (document.GetElement(parameter.AsElementId())
             if parameter is not None and parameter.StorageType == DB.StorageType.ElementId else None)
    if not isinstance(phase, DB.Phase) or not phase.UniqueId:
        return excluded(row["room_id"], "room_phase_identity_unavailable")
    row["level"] = {"id": str(level.Id.Value), "uid": str(level.UniqueId),
                    "project_elevation_ft": level.ProjectElevation}
    row["phase"] = {"id": str(phase.Id.Value), "uid": str(phase.UniqueId)}
    finishes = []
    for kind, selected in sorted(types.items()):
        elevation = level.ProjectElevation + selected["offset_ft"]
        if not finite(elevation):
            return excluded(row["room_id"], "finish_elevation_unavailable")
        finishes.append(dict(selected, kind=kind, project_elevation_ft=elevation))
    row["horizontal_finishes"] = finishes
    return row


def inspect_room(room, room_id, options, remaining):
    if not isinstance(room, DB.Architecture.Room):
        return excluded(room_id, "room_not_found"), 0
    if room.Location is None or room.Area <= 0:
        return excluded(room_id, "unplaced_or_unenclosed_room"), 0
    if room.LevelId.Value <= 0:
        return excluded(room_id, "missing_room_level"), 0
    boundary = room.GetBoundarySegments(options)
    if boundary is None or boundary.Count == 0:
        return excluded(room_id, "no_closed_boundary"), 0
    count = sum(loop.Count for loop in boundary)
    if count > remaining:
        return excluded(room_id, "segment_budget_exceeded"), 0

    profiles = List[DB.CurveLoop]()
    plane_height = None
    facts = []
    try:
        for segments in boundary:
            if segments.Count == 0:
                return excluded(room_id, "empty_boundary_loop"), count
            loop_facts = []
            profile = DB.CurveLoop()
            profiles.Add(profile)
            curves = [segment.GetCurve() for segment in segments]
            if any(curve is None or not curve.IsBound for curve in curves):
                return excluded(room_id, "unsupported_boundary_curve"), count
            starts = [curve.GetEndPoint(0) for curve in curves]
            ends = [curve.GetEndPoint(1) for curve in curves]
            if any(ends[i].DistanceTo(starts[(i + 1) % len(curves)]) > TOLERANCE_FT
                   for i in range(len(curves))):
                return excluded(room_id, "open_boundary_loop"), count
            for segment, curve, start, end in zip(segments, curves, starts, ends):
                profile.Append(curve)
                loop_facts.append({
                    "start_ft": xyz(start), "end_ft": xyz(end),
                    "curve_type": curve.GetType().Name,
                    "source_id": int(segment.ElementId.Value),
                    "linked_source_id": int(segment.LinkElementId.Value),
                })
            if not profile.HasPlane():
                return excluded(room_id, "nonplanar_boundary_loop"), count
            plane = profile.GetPlane()
            if abs(abs(plane.Normal.Z) - 1.0) > 1e-9:
                return excluded(room_id, "nonhorizontal_boundary_loop"), count
            if plane_height is not None and abs(plane.Origin.Z - plane_height) > TOLERANCE_FT:
                return excluded(room_id, "boundary_loops_on_different_planes"), count
            plane_height = plane.Origin.Z
            facts.append(loop_facts)
        if not DB.BoundaryValidation.IsValidHorizontalBoundary(profiles):
            return excluded(room_id, "invalid_boundary_topology"), count
        return {"room_id": room_id, "room_uid": room.UniqueId, "level_id": int(room.LevelId.Value),
                "status": "boundary_verified", "boundary_loops": facts}, count
    except Exception as exc:
        row = excluded(room_id, "boundary_read_failed")
        row["error_type"] = type(exc).__name__
        return row, count
    finally:
        for profile in profiles:
            profile.Dispose()


def read_boundaries(document, room_ids, finish_request=None):
    types = None
    identity = None
    if finish_request is not None:
        project = document.ProjectInformation
        if not document.PathName or project is None or not project.UniqueId:
            raise ValueError("Save the project before requesting a finish preview.")
        identity = {"project_uid": str(project.UniqueId),
                    "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}
        types = finish_types(document, finish_request)
    rows = []
    remaining = MAX_SEGMENTS
    options = DB.SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    try:
        for room_id in room_ids:
            try:
                room = document.GetElement(DB.ElementId(Int64(room_id)))
                row, used = inspect_room(room, room_id, options, remaining)
                remaining -= used
                if types and row["status"] == "boundary_verified":
                    row = room_finish_preview(document, room, row, types)
            except Exception as exc:
                row = excluded(room_id, "boundary_read_failed")
                row["error_type"] = type(exc).__name__
            rows.append(row)
    finally:
        options.Dispose()
    report = {
        "status": "reported" if all(row["status"] == "boundary_verified" for row in rows) else "incomplete",
        "rooms": rows,
        "boundary_location": "finish",
        "placement_available": False,
    }
    if identity:
        report["host_identity"] = identity
    return report


if doc is None or doc.IsFamilyDocument:
    result = {"error": "Open a Revit project before reading room boundaries."}
else:
    report = read_boundaries(doc, ids_from(inputs.get("room_ids")), inputs.get("horizontal_finishes"))
    result = {"payload": json.dumps(report, ensure_ascii=True, allow_nan=False)}
