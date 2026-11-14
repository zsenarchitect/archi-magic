# -*- coding: utf-8 -*-
"""Inventory selected room boundaries in Revit 2026 via run_query only.

For a room-view snapshot, pass inputs.room_views with padding_feet and rooms.
Each room selection specifies room_id, host_plan_id, families and requests.
Set families.elevation or families.section to type_id and template_id.
Set each request to kind=elevation and side (a zero-based finish edge), or
kind=section with cut (two XY points in feet) and look (a perpendicular XY vector).
For native preflight, pass the inputs emitted by room_views_preview.py.
"""

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
FINISH_FIELDS = {"horizontal_finishes", "wall_type_id", "wall_dimensions"}
TEXT = (str, type(u""))
MAX_VIEW_NAMES = 20000
MAX_ROOM_VIEWS = 12
MAX_VIEWS = 80
MAX_GEOMETRY_PAIRS = 20000
MAX_BYTES = 2 * 1024 * 1024
MAX_READ_MILLISECONDS = 30000


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
                facts_for_curve = {
                    "start_ft": xyz(start), "end_ft": xyz(end),
                    "curve_type": curve.GetType().Name,
                    "source_id": int(segment.ElementId.Value),
                    "linked_source_id": int(segment.LinkElementId.Value),
                }
                if facts_for_curve["curve_type"] == "Arc":
                    facts_for_curve["midpoint_ft"] = xyz(curve.Evaluate(0.5, True))
                    facts_for_curve["center_ft"] = xyz(curve.Center)
                    facts_for_curve["radius_ft"] = curve.Radius
                loop_facts.append(facts_for_curve)
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


def review_finish_types(document, finish_request, wall_type_id, wall_dimensions):
    wall_type = None
    if wall_dimensions is not None:
        if (wall_type_id is None or not isinstance(wall_dimensions, dict)
                or set(wall_dimensions) != {"height_ft", "base_offset_ft"}
                or any(type(value) not in (int, float) or not finite(value) for value in wall_dimensions.values())
                or not 0 < wall_dimensions["height_ft"] <= 30000
                or not -30000 <= wall_dimensions["base_offset_ft"] <= 30000):
            raise ValueError("Supply a wall type, positive wall height, and level-relative base offset in feet.")
    types = finish_types(document, finish_request) if finish_request is not None else {}
    if wall_type_id is not None:
        value, = ids_from([wall_type_id])
        element = document.GetElement(DB.ElementId(Int64(value)))
        if (not isinstance(element, DB.WallType) or not element.UniqueId or element.Kind != DB.WallKind.Basic
                or not finite(element.Width) or element.Width <= 0):
            raise ValueError("Select a basic finish wall type with a known positive width.")
        wall_type = {"type_id": str(value), "type_uid": str(element.UniqueId), "width_ft": element.Width}
        if wall_dimensions is not None:
            wall_type.update({key: float(value) + 0.0 for key, value in wall_dimensions.items()})
    return types, wall_type


def read_finish_rules(document, rules):
    if not isinstance(rules, dict) or set(rules) != {"parameter", "by_value"}:
        raise ValueError("Supply a room text parameter and its explicit finish mapping.")
    name, table = rules["parameter"], rules["by_value"]
    if (not isinstance(name, TEXT) or not 1 <= len(name) <= 120 or name != name.strip()
            or not isinstance(table, dict) or not 1 <= len(table) <= 100):
        raise ValueError("Supply a parameter name and 1 to 100 finish mapping rows.")
    reviewed = {}
    for value, choice in table.items():
        if (not isinstance(value, TEXT) or not 1 <= len(value) <= 120 or value != value.strip()
                or not isinstance(choice, dict) or set(choice) - FINISH_FIELDS
                or all(choice.get(field) is None for field in ("horizontal_finishes", "wall_type_id"))):
            raise ValueError("Every mapping row needs an exact value and at least one finish type.")
        reviewed[value] = review_finish_types(document, choice.get("horizontal_finishes"),
                                               choice.get("wall_type_id"), choice.get("wall_dimensions"))
    return name, reviewed


def mapped_room_finish(document, room, row, name, table):
    parameters = list(room.GetParameters(name))
    if len(parameters) != 1:
        reason = "finish_parameter_ambiguous" if parameters else "finish_parameter_missing"
        return excluded(row["room_id"], reason)
    parameter = parameters[0]
    if parameter.StorageType != DB.StorageType.String or not parameter.HasValue:
        return excluded(row["room_id"], "finish_parameter_value_unavailable")
    value = (parameter.AsString() or "").strip()
    if value not in table:
        return excluded(row["room_id"], "finish_parameter_value_unmapped")
    types, wall_type = table[value]
    row = room_finish_preview(document, room, row, types, wall_type)
    row["finish_rule"] = {"parameter": name, "parameter_id": str(parameter.Id.Value), "value": value}
    return row


def read_boundaries(document, room_ids, finish_request=None, wall_type_id=None, audit_existing=False,
                    wall_dimensions=None, finish_rules=None):
    has_direct_types = finish_request is not None or wall_type_id is not None or wall_dimensions is not None
    if finish_rules is not None and has_direct_types:
        raise ValueError("Choose direct finish types or room parameter rules.")
    finish_selected = has_direct_types or finish_rules is not None
    if type(audit_existing) is not bool or (audit_existing and not finish_selected):
        raise ValueError("Select finish types before requesting an existing-finish audit.")
    identity = None
    if finish_selected:
        project = document.ProjectInformation
        if not document.PathName or project is None or not project.UniqueId:
            raise ValueError("Save the project before requesting a finish preview.")
        identity = {"project_uid": str(project.UniqueId),
                    "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}
    name, table = read_finish_rules(document, finish_rules) if finish_rules is not None else (None, {})
    types, wall_type = review_finish_types(document, finish_request, wall_type_id, wall_dimensions)
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
                if finish_selected and row["status"] == "boundary_verified":
                    row = (mapped_room_finish(document, room, row, name, table) if name else
                           room_finish_preview(document, room, row, types, wall_type))
            except Exception as exc:
                row = excluded(room_id, "boundary_read_failed")
                row["error_type"] = type(exc).__name__
            rows.append(row)
    finally:
        options.Dispose()
    if audit_existing:
        from room_finish_provenance import audit_existing_finishes
        audit_existing_finishes(document, rows)
    report = {
        "status": "reported" if all(row["status"] == "boundary_verified"
                                   and row.get("wall_finish", {}).get("status", "reported") == "reported"
                                   and row.get("existing_finishes", {}).get("status") != "conflict"
                                   and row.get("existing_finishes", {}).get("nearby", {}).get(
                                       "status", "none_in_scope") == "none_in_scope"
                                   for row in rows) else "incomplete",
        "rooms": rows,
        "boundary_location": "finish",
        "placement_available": False,
    }
    if identity:
        report["host_identity"] = identity
    return report


def element_identity(element, versioned=False):
    if element is None or element.Id.Value <= 0 or not element.UniqueId:
        raise ValueError("A required native element identity is unavailable.")
    identity = {"id": str(element.Id.Value), "uid": element.UniqueId}
    if versioned:
        identity["version"] = str(element.VersionGuid)
    return identity


def native_element(document, raw):
    value = str(raw)
    if (not value or len(value) > 19 or any(ch not in "0123456789" for ch in value)
            or not 0 < int(value) <= 9223372036854775807):
        raise ValueError("Use a positive native element ID.")
    return document.GetElement(DB.ElementId(Int64(value)))


def phase_of(document, element, parameter_id):
    parameter = element.get_Parameter(parameter_id)
    if parameter is None or parameter.StorageType != DB.StorageType.ElementId:
        raise ValueError("A required room or plan phase is unavailable.")
    phase = document.GetElement(parameter.AsElementId())
    if not isinstance(phase, DB.Phase):
        raise ValueError("A required room or plan phase is unavailable.")
    return phase


def view_family(document, selection, family, view_type):
    if not isinstance(selection, dict):
        raise ValueError("Select a native view type and template for each requested family.")
    element_type = native_element(document, selection.get("type_id"))
    template = native_element(document, selection.get("template_id"))
    if not isinstance(element_type, DB.ViewFamilyType) or element_type.ViewFamily != family:
        raise ValueError("The selected view type belongs to a different family.")
    if not isinstance(template, DB.View) or not template.IsTemplate or template.ViewType != view_type:
        raise ValueError("The selected view template belongs to a different family.")
    return {"type": element_identity(element_type, True), "template": element_identity(template, True)}


def spend(budget, kind, count):
    budget[kind] -= count
    if budget[kind] < 0:
        raise ValueError("Room geometry exceeds the %s limit." % kind)


def face_vertices(face, budget):
    if not isinstance(face, DB.PlanarFace):
        raise ValueError("The room solid has a nonplanar face.")
    loops = face.GetEdgesAsCurveLoops()
    try:
        if loops.Count != 1:
            raise ValueError("A room solid face has multiple loops.")
        vertices = []
        for curve in loops[0]:
            spend(budget, "geometry visits", 1)
            if not isinstance(curve, DB.Line) or not curve.IsBound:
                raise ValueError("The room solid has a curved or unbounded edge.")
            vertices.append(xyz(curve.GetEndPoint(0)))
        if not vertices:
            raise ValueError("The room solid has an empty face.")
        return vertices
    finally:
        for loop in loops:
            loop.Dispose()


def vertex_index(point, footprint, budget):
    spend(budget, "intersection pairs", len(footprint))
    matches = [
        index for index, corner in enumerate(footprint)
        if math.hypot(corner[0] - point[0], corner[1] - point[1]) <= TOLERANCE_FT
    ]
    if len(matches) != 1:
        raise ValueError("A room solid corner does not match one finish boundary corner.")
    return matches[0]


def face_signature(vertices, footprint, bottom, top, budget):
    corners = set()
    for vertex in vertices:
        if abs(vertex[2] - bottom) <= TOLERANCE_FT:
            layer = 0
        elif abs(vertex[2] - top) <= TOLERANCE_FT:
            layer = 1
        else:
            raise ValueError("The room solid has an intermediate height.")
        corner = (vertex_index(vertex, footprint, budget), layer)
        if corner in corners:
            raise ValueError("A room solid face repeats a corner.")
        corners.add(corner)
    return corners


def coordinates(values, size):
    if (not isinstance(values, list) or len(values) != size
            or any(type(value) not in (int, float) or not -1e9 <= value <= 1e9 for value in values)):
        raise ValueError("The reviewed geometry has invalid coordinates.")
    return values


def inside_segment(solid, start, end):
    options = DB.SolidCurveIntersectionOptions()
    try:
        options.ResultType = DB.SolidCurveIntersectionMode.CurveSegmentsInside
        line = DB.Line.CreateBound(start, end)
        try:
            intersection = solid.IntersectWithCurve(line, options)
            try:
                if intersection.SegmentCount != 1:
                    raise ValueError("The proposed line has no unique interior segment.")
                segment = intersection.GetCurveSegment(0)
                try:
                    endpoints = [segment.GetEndPoint(0), segment.GetEndPoint(1)]
                    endpoints.sort(key=start.DistanceTo)
                    return endpoints
                finally:
                    segment.Dispose()
            finally:
                intersection.Dispose()
        finally:
            line.Dispose()
    finally:
        options.Dispose()


def check_elevation_ray(room, solid, view, footprint, sources, padding, budget):
    source = view.get("source")
    index = source.get("side") if isinstance(source, dict) else None
    if type(index) is not int or not 0 <= index < len(footprint):
        raise ValueError("The reviewed elevation has no valid finish side.")
    if source.get("finish_source") != {"id": sources[index]["id"], "uid": sources[index]["uid"]}:
        raise ValueError("The reviewed elevation finish identity has changed.")
    origin, look, _ = view_frame(view)
    if not room.IsPointInRoom(origin):
        raise ValueError("The proposed elevation marker is outside the native room.")
    first, second = footprint[index], footprint[(index + 1) % len(footprint)]
    target = DB.XYZ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2, origin.Z)
    depth = target.DistanceTo(origin)
    edge = [second[0] - first[0], second[1] - first[1]]
    if (depth <= TOLERANCE_FT or abs(edge[0] * look.X + edge[1] * look.Y) > TOLERANCE_FT
            or origin.Add(look.Multiply(depth)).DistanceTo(target) > TOLERANCE_FT
            or abs(coordinates([view.get("far_extent")], 1)[0] - padding - depth) > TOLERANCE_FT):
        raise ValueError("The proposed elevation ray does not point at the reviewed finish side.")
    half_width = math.hypot(*edge) / 2
    if (abs(coordinates(view.get("crop_min"), 2)[0] + padding + half_width) > TOLERANCE_FT
            or abs(coordinates(view.get("crop_max"), 2)[0] - padding - half_width) > TOLERANCE_FT):
        raise ValueError("The elevation crop does not match the native finish width.")
    spend(budget, "intersection pairs", 1)
    start, exit_point = inside_segment(solid, origin, target.Add(look.Multiply(0.01)))
    if start.DistanceTo(origin) > TOLERANCE_FT or exit_point.DistanceTo(target) > TOLERANCE_FT:
        raise ValueError("The proposed elevation ray hits a different native room boundary first.")
    return {"kind": "elevation", "name": view["name"], "room_id": view["room_id"],
            "side": index, "finish_source": source["finish_source"], "first_hit": xyz(exit_point)}


def view_frame(view):
    origin = DB.XYZ(*coordinates(view.get("origin"), 3))
    look = DB.XYZ(*coordinates(view.get("look"), 3))
    right = DB.XYZ(*coordinates(view.get("right"), 3))
    if (abs(look.Z) > TOLERANCE_FT or abs(look.GetLength() - 1) > TOLERANCE_FT
            or right.DistanceTo(DB.XYZ(look.Y, -look.X, 0)) > TOLERANCE_FT):
        raise ValueError("The reviewed view needs a consistent horizontal frame.")
    return origin, look, right


def check_section_slice(room, solid, view, footprint, padding, budget):
    origin, look, right = view_frame(view)
    source = view["source"]
    cut = source.get("cut")
    if not isinstance(cut, list) or len(cut) != 2:
        raise ValueError("The section has no reviewed cut line.")
    reviewed_look = DB.XYZ(*(coordinates(source.get("look"), 2) + [0]))
    if reviewed_look.GetLength() <= TOLERANCE_FT or look.DistanceTo(
            reviewed_look.Multiply(1 / reviewed_look.GetLength())) > TOLERANCE_FT:
        raise ValueError("The section looks away from the reviewed direction.")
    offsets = []
    for point in cut:
        x, y = coordinates(point, 2)
        delta_x, delta_y = x - origin.X, y - origin.Y
        if abs(delta_x * look.X + delta_y * look.Y) > TOLERANCE_FT:
            raise ValueError("The section is off the reviewed cut plane.")
        offsets.append(delta_x * right.X + delta_y * right.Y)
    lower = coordinates(view.get("crop_min"), 2)[0] + padding
    upper = coordinates(view.get("crop_max"), 2)[0] - padding
    if (not lower < 0 < upper or min(offsets) > lower + TOLERANCE_FT
            or max(offsets) < upper - TOLERANCE_FT or not room.IsPointInRoom(origin)):
        raise ValueError("The proposed section has no interior room slice.")
    depths = [(x - origin.X) * look.X + (y - origin.Y) * look.Y for x, y in footprint]
    if (min(depths) >= -TOLERANCE_FT or max(depths) <= TOLERANCE_FT
            or abs(coordinates([view.get("far_extent")], 1)[0] - padding - max(depths)) > TOLERANCE_FT):
        raise ValueError("The section depth does not match the native room on the reviewed side.")
    spend(budget, "intersection pairs", 1)
    start, end = inside_segment(solid, origin.Add(right.Multiply(lower - 0.01)),
                                origin.Add(right.Multiply(upper + 0.01)))
    if (start.DistanceTo(origin.Add(right.Multiply(lower))) > TOLERANCE_FT
            or end.DistanceTo(origin.Add(right.Multiply(upper))) > TOLERANCE_FT):
        raise ValueError("The section crop does not match the measured native room slice.")
    return {"kind": "section", "name": view["name"], "room_id": view["room_id"],
            "slice_start": xyz(start), "slice_end": xyz(end)}


def measure_prism(calculator, room, footprint, sources, budget, views, padding):
    evidence = calculator.CalculateSpatialElementGeometry(room)
    try:
        solid = evidence.GetGeometry()
        if solid.Faces.Size != len(footprint) + 2 or solid.Volume <= 0:
            raise ValueError("The room solid does not have the expected prismatic faces.")
        horizontal = []
        sides = []
        for face in solid.Faces:
            spend(budget, "geometry visits", 1)
            vertices = face_vertices(face, budget)
            normal_z = abs(face.FaceNormal.Z)
            if normal_z >= 1 - 1e-9:
                horizontal.append(vertices)
            elif normal_z <= 1e-9:
                sides.append((face, vertices))
            else:
                raise ValueError("The room solid has a sloped face.")
        if len(horizontal) != 2:
            raise ValueError("The room solid needs one base and one top face.")
        horizontal.sort(key=lambda vertices: vertices[0][2])
        bottom, top = (vertices[0][2] for vertices in horizontal)
        if top - bottom <= 2 * TOLERANCE_FT:
            raise ValueError("The measured room solid has no usable height.")
        for layer, vertices in enumerate(horizontal):
            expected = {(index, layer) for index in range(len(footprint))}
            if face_signature(vertices, footprint, bottom, top, budget) != expected:
                raise ValueError("The room solid base or top does not match the footprint.")
        pairs = {
            frozenset((index, (index + 1) % len(footprint))): index
            for index in range(len(footprint))
        }
        seen = set()
        for face, vertices in sides:
            signature = face_signature(vertices, footprint, bottom, top, budget)
            edge = frozenset(index for index, _ in signature)
            index = pairs.get(edge)
            if (index is None or index in seen
                    or signature != {(corner, layer) for corner in edge for layer in (0, 1)}):
                raise ValueError("A room solid side does not match a distinct finish edge.")
            seen.add(index)
            subfaces = evidence.GetBoundaryFaceInfo(face)
            try:
                if subfaces is None or subfaces.Count != 1:
                    raise ValueError("A room side has ambiguous finish subfaces.")
                spend(budget, "geometry visits", 1)
                subface = subfaces[0]
                source = subface.SpatialBoundaryElement
                if (not subface.Valid or subface.SubfaceType != DB.SubfaceType.Side
                        or source.HostElementId.Value != int(sources[index]["id"])
                        or source.LinkedElementId.Value > 0):
                    raise ValueError("A room side does not match its native finish source.")
                if face_signature(face_vertices(subface.GetSubface(), budget),
                                  footprint, bottom, top, budget) != signature:
                    raise ValueError("A native finish subface does not span its room side.")
            finally:
                if subfaces is not None:
                    for subface in subfaces:
                        subface.Dispose()
        if len(seen) != len(footprint):
            raise ValueError("The room solid is missing a finish side.")
        checks = []
        for view in views:
            origin_z = coordinates(view.get("origin"), 3)[2]
            lower = coordinates(view.get("crop_min"), 2)[1]
            upper = coordinates(view.get("crop_max"), 2)[1]
            if (abs(origin_z + lower + padding - bottom) > TOLERANCE_FT
                    or abs(origin_z + upper - padding - top) > TOLERANCE_FT):
                raise ValueError("The proposed crop height does not match the native room solid.")
            if view["kind"] == "elevation":
                check = check_elevation_ray(room, solid, view, footprint, sources, padding, budget)
            else:
                check = check_section_slice(room, solid, view, footprint, padding, budget)
            checks.append(check)
        return [bottom, top], checks
    finally:
        evidence.Dispose()


def measure_room_view_source(document, calculator, room, boundary, selection, budget, views, padding):
    loops = boundary["boundary_loops"]
    if len(loops) != 1 or len(loops[0]) < 3:
        raise ValueError("Room views need one closed footprint with at least three sides.")
    footprint = []
    sources = []
    for side in loops[0]:
        if side["curve_type"] != "Line" or side["linked_source_id"] > 0:
            raise ValueError("Room views need straight finish edges with native sources.")
        footprint.append(side["start_ft"][:2])
        source = native_element(document, side["source_id"])
        if isinstance(source, DB.RevitLinkInstance):
            raise ValueError("Linked finish sources require separate qualification.")
        sources.append(element_identity(source, True))
    level = document.GetElement(room.LevelId)
    phase = phase_of(document, room, DB.BuiltInParameter.ROOM_PHASE_ID)
    host = native_element(document, selection.get("host_plan_id"))
    if (not isinstance(host, DB.ViewPlan) or host.IsTemplate
            or host.ViewType != DB.ViewType.FloorPlan or host.GenLevel is None
            or host.GenLevel.Id.Value != room.LevelId.Value):
        raise ValueError("Select a floor plan on the room level.")
    host_phase = phase_of(document, host, DB.BuiltInParameter.VIEW_PHASE)
    if host_phase.Id.Value != phase.Id.Value:
        raise ValueError("The host plan and room must use the same phase.")
    if room.DesignOption is not None or host.DesignOption is not None:
        raise ValueError("Room views in design options require separate qualification.")
    requests = selection["requests"]
    selected = selection.get("families")
    if not isinstance(selected, dict):
        raise ValueError("Select the view type and template for each requested family.")
    families = {}
    for kind, family, view_type in (
        ("elevation", DB.ViewFamily.Elevation, DB.ViewType.Elevation),
        ("section", DB.ViewFamily.Section, DB.ViewType.Section),
    ):
        if any(request["kind"] == kind for request in requests):
            families[kind] = view_family(document, selected.get(kind), family, view_type)
    for view in views:
        kind = view["kind"]
        source = view.get("source")
        expected = families.get(kind)
        if (view.get("room_uid") != room.UniqueId or expected is None
                or view.get("type") != expected["type"] or view.get("template") != expected["template"]
                or not isinstance(source, dict)):
            raise ValueError("The reviewed room or view family identity has changed.")
        keys = ("side",) if kind == "elevation" else ("cut", "look")
        if not any(request["kind"] == kind and all(request.get(key) == source.get(key) for key in keys)
                   for request in requests):
            raise ValueError("The reviewed view has no matching source request.")
    heights, checks = measure_prism(calculator, room, footprint, sources, budget, views, padding)
    snapshot = dict(element_identity(room, True), number=room.Number,
                level=element_identity(level), phase=element_identity(phase),
                design_option=None, host_plan=element_identity(host, True),
                families=families, footprint=footprint, heights=heights,
                finish_sources=sources, requests=requests,
                geometry_status="prismatic_finish_verified")
    return snapshot, checks


def read_room_view_snapshot(document, options, requested_views=None):
    from System.Diagnostics import Stopwatch

    clock = Stopwatch.StartNew()
    if not document.PathName or document.ProjectInformation is None:
        raise ValueError("Open a saved Revit project.")
    project_uid = document.ProjectInformation.UniqueId
    if not project_uid:
        raise ValueError("The saved Revit project has no project identity.")
    selected = options.get("rooms")
    if not isinstance(selected, list) or not 1 <= len(selected) <= MAX_ROOMS:
        raise ValueError("Select between 1 and 20 rooms.")
    request_count = 0
    for room in selected:
        if not isinstance(room, dict):
            raise ValueError("Each selected room must specify its native IDs and view requests.")
        requests = room.get("requests")
        if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_ROOM_VIEWS:
            raise ValueError("Select between 1 and 12 views per room.")
        if any(not isinstance(request, dict) or request.get("kind") not in ("elevation", "section")
               for request in requests):
            raise ValueError("Each view request must identify an elevation or section.")
        request_count += len(requests)
    if request_count > MAX_VIEWS:
        raise ValueError("Select no more than 80 total views.")
    padding = options.get("padding_feet")
    if type(padding) not in (int, float) or not 0 <= padding <= 1e9:
        raise ValueError("Set a finite, nonnegative crop padding in feet.")
    design_options = DB.FilteredElementCollector(document)
    try:
        design_options.OfClass(DB.DesignOption)
        if design_options.GetElementCount():
            raise ValueError("Projects with design options require separate view visibility qualification.")
    finally:
        design_options.Dispose()
    names = []
    views = DB.FilteredElementCollector(document)
    try:
        views.OfClass(DB.View)
        for index, view in enumerate(views):
            if index >= MAX_VIEW_NAMES or clock.ElapsedMilliseconds >= MAX_READ_MILLISECONDS:
                raise ValueError("The complete view-name inventory exceeds the read limit.")
            if not view.Name or not view.Name.strip():
                raise ValueError("A native view name is unavailable.")
            names.append(view.Name)
    finally:
        views.Dispose()

    boundary_options = DB.SpatialElementBoundaryOptions()
    boundary_options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    boundary_options.StoreFreeBoundaryFaces = True
    rooms = []
    checks = []
    seen = set()
    budget = {"geometry visits": MAX_SEGMENTS, "intersection pairs": MAX_GEOMETRY_PAIRS}
    try:
        calculator = DB.SpatialElementGeometryCalculator(document, boundary_options)
        try:
            for selection in selected:
                if clock.ElapsedMilliseconds >= MAX_READ_MILLISECONDS:
                    raise ValueError("Room view evidence exceeds the read time limit.")
                room = native_element(document, selection.get("room_id"))
                if not isinstance(room, DB.Architecture.Room) or room.Id.Value in seen:
                    raise ValueError("Select distinct native rooms.")
                seen.add(room.Id.Value)
                boundary, used = inspect_room(room, room.Id.Value, boundary_options, budget["geometry visits"])
                spend(budget, "geometry visits", used)
                if boundary["status"] != "boundary_verified":
                    raise ValueError("Room %s boundary evidence: %s." % (room.Id.Value, boundary["reason"]))
                views_for_room = requested_views.get(str(room.Id.Value), []) if requested_views else []
                snapshot, room_checks = measure_room_view_source(
                    document, calculator, room, boundary, selection, budget, views_for_room, padding)
                rooms.append(snapshot)
                checks.extend(room_checks)
        finally:
            calculator.Dispose()
    finally:
        boundary_options.Dispose()
    if clock.ElapsedMilliseconds >= MAX_READ_MILLISECONDS:
        raise ValueError("Room view evidence exceeds the read time limit.")
    snapshot = {
        "document_identity": {
            "project_uid": project_uid,
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest(),
        },
        "view_names": sorted(names),
        "view_names_complete": True,
        "padding_feet": padding,
        "rooms": rooms,
    }
    return snapshot, checks


def native_preflight(document, selection, preflight):
    if not isinstance(preflight, dict):
        raise ValueError("Supply the reviewed room view preflight input.")
    snapshot_json = preflight.get("snapshot_json")
    proposal_json = preflight.get("proposal_json")
    if (not hasattr(snapshot_json, "encode") or not hasattr(proposal_json, "encode")
            or len(snapshot_json) > MAX_BYTES or len(proposal_json) > MAX_BYTES):
        raise ValueError("The reviewed room view input exceeds the byte limit or is missing.")
    snapshot_bytes = snapshot_json.encode("utf-8")
    proposal_bytes = proposal_json.encode("utf-8")
    if len(snapshot_bytes) > MAX_BYTES or len(proposal_bytes) > MAX_BYTES:
        raise ValueError("The reviewed room view input exceeds the byte limit.")
    proposal_sha256 = hashlib.sha256(proposal_bytes).hexdigest()
    if proposal_sha256 != preflight.get("proposal_sha256"):
        raise ValueError("The reviewed room view proposal digest does not match.")
    proposal = json.loads(proposal_json)
    if not isinstance(proposal, dict) or proposal.get("snapshot_sha256") != hashlib.sha256(snapshot_bytes).hexdigest():
        raise ValueError("The reviewed room view snapshot digest does not match.")
    views = proposal.get("views")
    if not isinstance(views, list) or not 1 <= len(views) <= MAX_VIEWS:
        raise ValueError("The reviewed room view proposal has an invalid view count.")
    grouped = {}
    names = set()
    for view in views:
        if (not isinstance(view, dict) or view.get("kind") not in ("elevation", "section")
                or not hasattr(view.get("room_id"), "strip") or not view["room_id"].strip()
                or not hasattr(view.get("name"), "strip") or not view["name"].strip()
                or view["name"] in names):
            raise ValueError("The reviewed room view proposal contains an invalid or duplicate view.")
        grouped.setdefault(view["room_id"], []).append(view)
        names.add(view["name"])
    if any(len(group) > MAX_ROOM_VIEWS for group in grouped.values()):
        raise ValueError("The reviewed proposal exceeds the per-room view limit.")
    current, checks = read_room_view_snapshot(document, selection, grouped)
    if current != json.loads(snapshot_json):
        raise ValueError("The saved project room view evidence has changed. Rebuild the preview.")
    if len(checks) != len(views):
        raise ValueError("The native query did not verify every reviewed room view.")
    return {"status": "verified", "proposal_sha256": proposal_sha256,
            "checks": checks, "placement_available": False}


def run_query(document, args):
    if document is None or document.IsFamilyDocument:
        return {"error": "Open a Revit project before reading room boundaries."}
    if args.get("room_views") is None:
        report = read_boundaries(document, ids_from(args.get("room_ids")),
                                 args.get("horizontal_finishes"), args.get("wall_type_id"),
                                 args.get("audit_existing_finishes", False), args.get("wall_dimensions"),
                                 args.get("finish_rules"))
        return {"payload": json.dumps(report, ensure_ascii=True, allow_nan=False)}
    try:
        selection = args["room_views"]
        if not isinstance(selection, dict):
            raise ValueError("Provide room view selections as an object.")
        preflight = args.get("room_view_preflight")
        if preflight is None:
            snapshot, _ = read_room_view_snapshot(document, selection)
            payload = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            response = {"status": "reported", "payload": payload}
        else:
            response = native_preflight(document, selection, preflight)
            payload = json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(payload) > MAX_BYTES:
            raise ValueError("Room view evidence exceeds the read byte limit.")
        return response
    except ValueError as exc:
        return {"status": "blocked", "reason": str(exc)}
    except Exception as exc:
        return {"status": "blocked", "reason": "Native room view read failed.", "error_type": type(exc).__name__}


if "doc" in globals() and "inputs" in globals():
    result = run_query(doc, inputs)
