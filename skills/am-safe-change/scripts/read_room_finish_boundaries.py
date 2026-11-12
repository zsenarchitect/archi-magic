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
MAX_WALL_SEGMENTS = 200
MAX_INSERTS_PER_WALL = 100
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


def visible_in_phase(element, phase_id):
    return (element.DesignOption is None and element.GetPhaseStatus(phase_id) in
            (DB.ElementOnPhaseStatus.New, DB.ElementOnPhaseStatus.Existing))


def door_interval(door, start, axis):
    box = door.get_BoundingBox(None)
    if box is None or not box.Enabled or not box.Transform.IsIdentity:
        return None
    low, high = xyz(box.Min), xyz(box.Max)
    if any(not (finite(a) and finite(b) and a <= b) for a, b in zip(low, high)):
        return None
    stations = [(x - start[0]) * axis[0] + (y - start[1]) * axis[1]
                for x in (low[0], high[0]) for y in (low[1], high[1])]
    return (min(stations), max(stations)) if all(finite(value) for value in stations) else None


def boundary_cuts_footprint(edge, bounds):
    """Test whether an outline edge crosses the interior of a local rectangle."""
    start, end = edge
    enter, leave = 0.0, 1.0
    for a, b, limits in zip(start, end, bounds):
        low, high = limits
        if a == b:
            if not low < a < high:
                return False
            continue
        first, last = sorted(((low - a) / (b - a), (high - a) / (b - a)))
        enter, leave = max(enter, first), min(leave, last)
        if enter >= leave:
            return False
    return True


def finish_centerlines(outline, start, axis, spans, width):
    normal = (-axis[1], axis[0])

    def local(point):
        dx, dy = point[0] - start[0], point[1] - start[1]
        return dx * axis[0] + dy * axis[1], dx * normal[0] + dy * normal[1]

    edges = [(local(a), local(b)) for a, b in outline]
    result = []
    for low, high in spans:
        fits = []
        for side in (-1, 1):
            y0, y1 = sorted((0.0, width * side))
            bounds = ((low + TOLERANCE_FT, high - TOLERANCE_FT),
                      (y0 + TOLERANCE_FT, y1 - TOLERANCE_FT))
            if any(a >= b for a, b in bounds) or any(boundary_cuts_footprint(edge, bounds) for edge in edges):
                continue
            mid_x, mid_y = (low + high) / 2.0, width * side / 2.0
            # Odd parity across all loops places the point in the room, including rooms with holes.
            crossings = sum(1 for (ax, ay), (bx, by) in edges
                            if (ay > mid_y) != (by > mid_y)
                            and mid_x < ax + (mid_y - ay) * (bx - ax) / (by - ay))
            if crossings % 2:
                fits.append(side)
        record = {"span_ft": [low, high]}
        if len(fits) == 1:
            offset = width * fits[0] / 2.0
            points = [[start[i] + station * axis[i] + offset * normal[i] for i in (0, 1)] + [start[2]]
                      for station in (low, high)]
            record.update(status="footprint_previewed", centerline_ft=points)
        else:
            record.update(status="excluded", reason="finish_width_does_not_fit_on_one_side")
        result.append(record)
    return result


def available_spans(start, end, blocked, short_curve):
    spans, cursor = [], start
    for low, high in sorted(blocked):
        low, high = max(start, low), min(end, high)
        if low >= high:
            continue
        if low - cursor > short_curve:
            spans.append([cursor, low])
        cursor = max(cursor, high)
    if end - cursor > short_curve:
        spans.append([cursor, end])
    return spans


def footprint_corners(centerline, half_width):
    a, b = centerline
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    nx, ny = -dy * half_width / length, dx * half_width / length
    return [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny),
            (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]


def clip_footprint(points, boundary, direction):
    clipped = []
    for a, b in zip(points[-1:] + points[:-1], points):
        inside_a = direction * (a[1] - boundary) >= 0
        inside_b = direction * (b[1] - boundary) >= 0
        if inside_a != inside_b:
            fraction = (boundary - a[1]) / (b[1] - a[1])
            clipped.append((a[0] + fraction * (b[0] - a[0]), boundary))
        if inside_b:
            clipped.append(b)
    return clipped


def overlapping_stations(corners, start, axis, half_width):
    ux, uy = axis
    offsets = [(x - start[0], y - start[1]) for x, y in corners]
    points = [(x * ux + y * uy, -x * uy + y * ux) for x, y in offsets]
    if (min(y for _, y in points) >= half_width - TOLERANCE_FT
            or max(y for _, y in points) <= -half_width + TOLERANCE_FT):
        return None
    clipped = clip_footprint(clip_footprint(points, -half_width, 1), half_width, -1)
    return (min(x for x, _ in clipped), max(x for x, _ in clipped)) if clipped else None


def trim_overlapping_finishes(segments, width, short_curve):
    rows = [segment["wall_finish"] for segment in segments if "finish_footprints" in segment["wall_finish"]]
    candidates = [footprint for row in rows for footprint in row["finish_footprints"]
                  if footprint["status"] == "footprint_previewed"]
    if len(candidates) > MAX_WALL_SEGMENTS:
        return False
    # Favor longer pieces, with a geometry tie-break independent of room loop order.
    candidates.sort(key=lambda item: (item["span_ft"][0] - item["span_ft"][1],
                                      sorted(item["centerline_ft"])))
    half_width = width / 2.0
    accepted = []
    replacements = {}
    for candidate in candidates:
        a, b = candidate["centerline_ft"]
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        axis = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
        blocked = []
        for corners in accepted:
            interval = overlapping_stations(corners, a, axis, half_width)
            if interval and min(length, interval[1]) - max(0.0, interval[0]) > TOLERANCE_FT:
                blocked.append(interval)
        if not blocked:
            if len(accepted) >= MAX_WALL_SEGMENTS:
                return False
            accepted.append(footprint_corners(candidate["centerline_ft"], half_width))
            continue
        pieces = []
        for low, high in available_spans(0.0, length, blocked, short_curve):
            if len(accepted) >= MAX_WALL_SEGMENTS:
                return False
            start = candidate["span_ft"][0]
            points = [[a[0] + station * axis[0], a[1] + station * axis[1], a[2]] for station in (low, high)]
            pieces.append(dict(candidate, span_ft=[start + low, start + high], centerline_ft=points,
                               original_span_ft=candidate["span_ft"]))
            accepted.append(footprint_corners(points, half_width))
        replacements[id(candidate)] = pieces or [{
            "status": "excluded", "reason": "overlap_consumes_finish_span", "span_ft": candidate["span_ft"],
        }]
    for row in rows:
        row["finish_footprints"] = [piece for original in row["finish_footprints"]
                                     for piece in replacements.get(id(original), [original])]
        if any(piece["status"] == "excluded" for piece in row["finish_footprints"]):
            row["status"] = "incomplete"
    return True


def wall_segment_preview(document, segment, phase_id, short_curve, outline, width):
    if segment["linked_source_id"] > 0 or segment["curve_type"] != "Line" or segment["source_id"] <= 0:
        return {"status": "excluded", "reason": "boundary_not_a_straight_host_wall"}
    wall = document.GetElement(DB.ElementId(Int64(segment["source_id"])))
    if (not isinstance(wall, DB.Wall) or not wall.UniqueId or wall.IsStackedWallMember
            or wall.WallType.Kind != DB.WallKind.Basic or wall.CrossSection != DB.WallCrossSection.Vertical
            or not visible_in_phase(wall, phase_id)):
        return {"status": "excluded", "reason": "wall_geometry_or_phase_requires_review"}
    start, end = segment["start_ft"], segment["end_ft"]
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if not finite(length) or length <= short_curve:
        return {"status": "excluded", "reason": "wall_boundary_too_short"}
    axis = (dx / length, dy / length)
    insert_ids = wall.FindInserts(True, True, True, True)
    if len(insert_ids) > MAX_INSERTS_PER_WALL:
        return {"status": "excluded", "reason": "wall_insert_limit"}
    doors = []
    for identifier in insert_ids:
        door = document.GetElement(identifier)
        if (not isinstance(door, DB.FamilyInstance) or not door.UniqueId or door.Category is None
                or door.Category.Id.Value != int(DB.BuiltInCategory.OST_Doors)
                or door.Host is None or door.Host.Id.Value != wall.Id.Value or not visible_in_phase(door, phase_id)):
            return {"status": "excluded", "reason": "wall_insert_requires_review"}
        interval = door_interval(door, start, axis)
        if interval is None or interval[1] - interval[0] <= TOLERANCE_FT:
            return {"status": "excluded", "reason": "door_bounds_unavailable"}
        low, high = interval
        if high < -TOLERANCE_FT or low > length + TOLERANCE_FT:
            continue
        if low <= TOLERANCE_FT or high >= length - TOLERANCE_FT:
            return {"status": "excluded", "reason": "door_crosses_or_touches_boundary_end"}
        doors.append({"id": str(identifier.Value), "uid": str(door.UniqueId), "start_ft": low, "end_ft": high})
    doors.sort(key=lambda item: (item["start_ft"], item["end_ft"], item["id"]))
    spans = available_spans(0.0, length, [(door["start_ft"], door["end_ft"]) for door in doors], short_curve)
    footprints = finish_centerlines(outline, start, axis, spans, width)
    status = "incomplete" if any(item["status"] == "excluded" for item in footprints) else "spans_previewed"
    return {"status": status, "wall_uid": str(wall.UniqueId), "door_envelopes": doors,
            "boundary_spans_ft": spans, "finish_footprints": footprints}


def room_finish_preview(document, room, row, types, wall_type):
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
    if wall_type:
        short_curve = document.Application.ShortCurveTolerance
        segments = [segment for loop in row["boundary_loops"] for segment in loop]
        if not finite(short_curve) or short_curve <= 0:
            row["wall_finish"] = {"status": "excluded", "reason": "short_curve_tolerance_unavailable"}
        elif len(segments) > MAX_WALL_SEGMENTS:
            row["wall_finish"] = {"status": "excluded", "reason": "wall_boundary_segment_limit"}
        elif any(segment["curve_type"] != "Line" for segment in segments):
            row["wall_finish"] = {"status": "excluded", "reason": "curved_room_outline_requires_review"}
        else:
            row["wall_finish"] = dict(wall_type, status="reported")
            outline = [(segment["start_ft"], segment["end_ft"]) for segment in segments]
            for segment in segments:
                try:
                    result = wall_segment_preview(document, segment, phase.Id, short_curve, outline, wall_type["width_ft"])
                except Exception as exc:
                    result = {"status": "excluded", "reason": "wall_boundary_read_failed",
                              "error_type": type(exc).__name__}
                segment["wall_finish"] = result
            if not trim_overlapping_finishes(segments, wall_type["width_ft"], short_curve):
                row["wall_finish"].update(status="excluded", reason="wall_finish_piece_limit")
                for segment in segments:
                    segment["wall_finish"].pop("finish_footprints", None)
            elif any(segment["wall_finish"]["status"] != "spans_previewed" for segment in segments):
                row["wall_finish"]["status"] = "incomplete"
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


def read_boundaries(document, room_ids, finish_request=None, wall_type_id=None):
    types = None
    identity = None
    wall_type = None
    if finish_request is not None or wall_type_id is not None:
        project = document.ProjectInformation
        if not document.PathName or project is None or not project.UniqueId:
            raise ValueError("Save the project before requesting a finish preview.")
        identity = {"project_uid": str(project.UniqueId),
                    "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}
        types = finish_types(document, finish_request) if finish_request is not None else {}
    if wall_type_id is not None:
        value, = ids_from([wall_type_id])
        element = document.GetElement(DB.ElementId(Int64(value)))
        if (not isinstance(element, DB.WallType) or not element.UniqueId or element.Kind != DB.WallKind.Basic
                or not finite(element.Width) or element.Width <= 0):
            raise ValueError("Select a basic finish wall type with a known positive width.")
        wall_type = {"type_id": str(value), "type_uid": str(element.UniqueId), "width_ft": element.Width}
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
                if types is not None and row["status"] == "boundary_verified":
                    row = room_finish_preview(document, room, row, types, wall_type)
            except Exception as exc:
                row = excluded(room_id, "boundary_read_failed")
                row["error_type"] = type(exc).__name__
            rows.append(row)
    finally:
        options.Dispose()
    report = {
        "status": "reported" if all(row["status"] == "boundary_verified"
                                   and row.get("wall_finish", {}).get("status", "reported") == "reported"
                                   for row in rows) else "incomplete",
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
    report = read_boundaries(doc, ids_from(inputs.get("room_ids")),
                             inputs.get("horizontal_finishes"), inputs.get("wall_type_id"))
    result = {"payload": json.dumps(report, ensure_ascii=True, allow_nan=False)}
