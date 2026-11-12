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
TOLERANCE_FT = 1e-6
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


def read_boundaries(document, room_ids):
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
            except Exception as exc:
                row = excluded(room_id, "boundary_read_failed")
                row["error_type"] = type(exc).__name__
            rows.append(row)
    finally:
        options.Dispose()
    return {
        "status": "reported" if all(row["status"] == "boundary_verified" for row in rows) else "incomplete",
        "rooms": rows,
        "boundary_location": "finish",
        "placement_available": False,
    }


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


if doc is None or doc.IsFamilyDocument:
    result = {"error": "Open a Revit project before reading room boundaries."}
elif inputs.get("room_views") is not None:
    try:
        selection = inputs["room_views"]
        if not isinstance(selection, dict):
            raise ValueError("Provide room view selections as an object.")
        preflight = inputs.get("room_view_preflight")
        if preflight is None:
            snapshot, _ = read_room_view_snapshot(doc, selection)
            payload = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            response = {"status": "reported", "payload": payload}
        else:
            response = native_preflight(doc, selection, preflight)
            payload = json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(payload) > MAX_BYTES:
            raise ValueError("Room view evidence exceeds the read byte limit.")
        result = response
    except ValueError as exc:
        result = {"status": "blocked", "reason": str(exc)}
    except Exception as exc:
        result = {"status": "blocked", "reason": "Native room view read failed.", "error_type": type(exc).__name__}
else:
    report = read_boundaries(doc, ids_from(inputs.get("room_ids")))
    result = {"payload": json.dumps(report, ensure_ascii=True)}
