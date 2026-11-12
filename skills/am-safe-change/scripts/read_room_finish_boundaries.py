# -*- coding: utf-8 -*-
"""Inventory selected room boundaries in Revit 2026 via run_query only.

For a room-view snapshot, pass inputs.room_views with padding_feet and rooms.
Each room selection specifies room_id, host_plan_id, families and requests.
Set families.elevation or families.section to type_id and template_id.
Set each request to kind=elevation and side (a zero-based finish edge), or
kind=section with cut (two XY points in feet) and look (a perpendicular XY vector).
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


def measure_prism(calculator, room, footprint, sources, budget):
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
        return [bottom, top]
    finally:
        evidence.Dispose()


def measure_room_view_source(document, calculator, room, boundary, selection, budget):
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
    heights = measure_prism(calculator, room, footprint, sources, budget)
    return dict(element_identity(room, True), number=room.Number,
                level=element_identity(level), phase=element_identity(phase),
                design_option=None, host_plan=element_identity(host, True),
                families=families, footprint=footprint, heights=heights,
                finish_sources=sources, requests=requests,
                geometry_status="prismatic_finish_verified")


def read_room_view_snapshot(document, options):
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
                rooms.append(measure_room_view_source(document, calculator, room, boundary, selection, budget))
        finally:
            calculator.Dispose()
    finally:
        boundary_options.Dispose()
    if clock.ElapsedMilliseconds >= MAX_READ_MILLISECONDS:
        raise ValueError("Room view evidence exceeds the read time limit.")
    return {
        "document_identity": {
            "project_uid": project_uid,
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest(),
        },
        "view_names": sorted(names),
        "view_names_complete": True,
        "padding_feet": padding,
        "rooms": rooms,
    }


if doc is None or doc.IsFamilyDocument:
    result = {"error": "Open a Revit project before reading room boundaries."}
elif inputs.get("room_views") is not None:
    try:
        selection = inputs["room_views"]
        if not isinstance(selection, dict):
            raise ValueError("Provide room view selections as an object.")
        snapshot = read_room_view_snapshot(doc, selection)
        payload = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(payload) > MAX_BYTES:
            raise ValueError("Room view evidence exceeds the read byte limit.")
        result = {"status": "reported", "payload": payload}
    except ValueError as exc:
        result = {"status": "blocked", "reason": str(exc)}
    except Exception as exc:
        result = {"status": "blocked", "reason": "Native room view read failed.", "error_type": type(exc).__name__}
else:
    report = read_boundaries(doc, ids_from(inputs.get("room_ids")))
    result = {"payload": json.dumps(report, ensure_ascii=True)}
