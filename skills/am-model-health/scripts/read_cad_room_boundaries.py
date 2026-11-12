"""Read the finish boundaries of existing host rooms near a CAD layout."""

import math
from time import monotonic

from pyrevit.api import DB

from read_linked_cad_layout import EVIDENCE_LIMIT, MM_PER_FOOT, element_id, encoded, sha256

MAX_ROOMS = 250
MAX_SEGMENTS = 5000
CLOSURE_TOLERANCE_MM = 0.001


def xyz_mm(point):
    values = tuple(float(value) * MM_PER_FOOT for value in (point.X, point.Y, point.Z))
    if not all(math.isfinite(value) for value in values):
        raise ValueError("invalid_room_coordinate")
    return tuple(round(value, 6) + 0.0 for value in values)


def boundary_source(document, segment):
    source_id, linked_id = segment.ElementId, segment.LinkElementId
    source = document.GetElement(source_id) if source_id.Value > 0 else None
    if source is None or not source.UniqueId:
        raise ValueError("unresolved_boundary_source")
    identity = (str(source_id.Value), str(source.UniqueId))
    if linked_id.Value <= 0:
        if isinstance(source, DB.RevitLinkInstance):
            raise ValueError("unresolved_linked_boundary_source")
        return (*identity, "", "")
    if not isinstance(source, DB.RevitLinkInstance):
        raise ValueError("unresolved_linked_boundary_source")
    linked = source.GetLinkDocument()
    element = linked.GetElement(linked_id) if linked is not None else None
    if element is None or not element.UniqueId:
        raise ValueError("unresolved_linked_boundary_source")
    return (*identity, str(linked_id.Value), str(element.UniqueId))


def boundary_edge(document, segment):
    try:
        source = boundary_source(document, segment)
        curve = segment.GetCurve()
        if curve is None:
            raise ValueError("unsupported_room_boundary_curve")
        try:
            if not curve.IsBound or not isinstance(curve, (DB.Line, DB.Arc)):
                raise ValueError("unsupported_room_boundary_curve")
            points = [xyz_mm(curve.GetEndPoint(0))]
            if isinstance(curve, DB.Arc):
                points.append(xyz_mm(curve.Evaluate(0.5, True)))
            points.append(xyz_mm(curve.GetEndPoint(1)))
            if points[0] == points[-1]:
                raise ValueError("degenerate_room_boundary_curve")
            return source, tuple(points)
        finally:
            curve.Dispose()
    finally:
        segment.Dispose()


def canonical_loop(edges):
    if len(edges) < 2:
        raise ValueError("incomplete_room_boundary_loop")
    starts = [points[0] for _, points in edges]
    if len(set(starts)) != len(starts) or any(
            math.dist(points[-1], starts[(index + 1) % len(edges)]) > CLOSURE_TOLERANCE_MM
            for index, (_, points) in enumerate(edges)):
        raise ValueError("open_or_repeated_room_boundary")

    def rotate(rows):
        first = min(range(len(rows)), key=lambda index: rows[index][1][0])
        return rows[first:] + rows[:first]

    reverse = [(source, points[::-1]) for source, points in reversed(edges)]
    return min(rotate(edges), rotate(reverse))


def read_room(document, candidate, options, deadline, remaining):
    room = document.GetElement(element_id(candidate["id"]))
    if (not isinstance(room, DB.Architecture.Room) or str(room.UniqueId) != candidate["uid"]
            or str(room.VersionGuid) != candidate["version_guid"] or room.Location is None):
        raise ValueError("room_identity_changed")
    if room.DesignOption is not None:
        raise ValueError("room_design_option_requires_review")
    level = document.GetElement(room.LevelId)
    parameter = room.get_Parameter(DB.BuiltInParameter.ROOM_PHASE_ID)
    phase = (document.GetElement(parameter.AsElementId()) if parameter is not None
             and parameter.StorageType == DB.StorageType.ElementId and parameter.HasValue else None)
    if not isinstance(level, DB.Level) or not level.UniqueId or not isinstance(phase, DB.Phase) or not phase.UniqueId:
        raise ValueError("unresolved_room_level_or_phase")
    area = float(room.Area) * MM_PER_FOOT ** 2
    native_loops = room.GetBoundarySegments(options)
    has_loops = native_loops is not None and len(native_loops) > 0
    if not math.isfinite(area) or area < 0 or (area > 0) != has_loops:
        raise ValueError("inconsistent_room_area_and_boundary")
    loops, sources = [], set()
    for native_loop in native_loops if has_loops else ():
        edges = []
        for segment in native_loop:
            if remaining[0] == 0 or monotonic() >= deadline:
                raise ValueError("room_boundary_budget")
            remaining[0] -= 1
            edge = boundary_edge(document, segment)
            sources.add(edge[0])
            edges.append(edge)
        loops.append(canonical_loop(edges))
    return {"id": candidate["id"], "uid": candidate["uid"], "near": candidate["near"],
            "level_id": str(room.LevelId.Value), "level_uid": str(level.UniqueId),
            "phase_id": str(phase.Id.Value), "phase_uid": str(phase.UniqueId),
            "location_mm": xyz_mm(room.Location.Point), "native_area_mm2": area,
            "enclosed": bool(loops), "segment_count": sum(map(len, loops)),
            "boundary_sha256": sha256(encoded(sorted(loops), EVIDENCE_LIMIT)),
            "boundary_sources": sorted(sources)}


def read_existing_room_boundaries(document, scan, deadline):
    rows, unresolved = [], []
    candidates = [row for row in scan["candidates"] if row["category"] == "OST_Rooms"]
    limit = "rooms" if len(candidates) > MAX_ROOMS else None
    if candidates:
        options = DB.SpatialElementBoundaryOptions()
        options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
        remaining = [MAX_SEGMENTS]
        try:
            for candidate in candidates[:MAX_ROOMS]:
                if monotonic() >= deadline or remaining[0] == 0:
                    limit = "time" if monotonic() >= deadline else "segments"
                    break
                try:
                    rows.append(read_room(document, candidate, options, deadline, remaining))
                except Exception as error:
                    unresolved.append({"id": candidate["id"],
                                       "reason": str(error) if type(error) is ValueError else type(error).__name__})
        finally:
            options.Dispose()
        if monotonic() >= deadline:
            limit = "time"
    incomplete = bool(limit or unresolved or scan["status"] == "incomplete")
    return {"status": "incomplete" if incomplete else "measured" if rows else "none_found",
            "limit": limit, "rooms": rows, "unresolved": unresolved}
