# -*- coding: utf-8 -*-
"""Read a Toposolid conversion source or an existing floor for comparison."""

import json
import math

from System.Diagnostics import Stopwatch
from pyrevit.api import DB

from read_toposolid_vertices import (
    MAX_PAYLOAD_BYTES, MAX_READ_MS, element_id, finite_number, project_identity, xyz,
)


MAX_SKETCH_CURVES = 512
MAX_TOP_FACES = 512
MAX_FACE_EDGES = 4096
MAX_TRIANGLES = 3000
GEOMETRY_EPSILON_FT = 1e-7


def check_time(clock):
    if clock.ElapsedMilliseconds >= MAX_READ_MS:
        raise ValueError("Toposolid floor region read exceeded {} ms.".format(MAX_READ_MS))


def identity(element):
    if not element.UniqueId:
        raise ValueError("A floor region dependency has no stable element identity.")
    return {"id": str(element.Id.Value), "uid": str(element.UniqueId),
            "version": str(element.VersionGuid)}


def checked_point(point):
    result = xyz(point)
    if not all(finite_number(value) for value in result):
        raise ValueError("A floor region coordinate is non-finite or outside the supported range.")
    return result


def distance(first, second):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def read_profile(sketch, clock):
    loops, total, elevation = [], 0, None
    for loop in sketch.Profile:
        segments = []
        for curve in loop:
            check_time(clock)
            total += 1
            if total > MAX_SKETCH_CURVES:
                raise ValueError("The floor profile exceeds {} curves.".format(MAX_SKETCH_CURVES))
            if not curve.IsBound or not isinstance(curve, (DB.Line, DB.Arc)):
                raise ValueError("The floor profile needs bounded straight lines or circular arcs.")
            start, end = (checked_point(curve.GetEndPoint(i)) for i in (0, 1))
            points = [start, end]
            kind = "line"
            if isinstance(curve, DB.Arc):
                points.insert(1, checked_point(curve.Evaluate(0.5, True)))
                kind = "arc"
            if elevation is None:
                elevation = start[2]
            if (distance(start, end) <= GEOMETRY_EPSILON_FT
                    or any(abs(point[2] - elevation) > GEOMETRY_EPSILON_FT for point in points)):
                raise ValueError("The selected region needs nondegenerate horizontal sketch curves.")
            if segments and distance(segments[-1]["points_ft"][-1], start) > GEOMETRY_EPSILON_FT:
                raise ValueError("A floor profile loop is disconnected.")
            segments.append({"kind": kind, "points_ft": points})
        if not segments or distance(segments[-1]["points_ft"][-1], segments[0]["points_ft"][0]) > GEOMETRY_EPSILON_FT:
            raise ValueError("A floor profile loop is empty or open.")
        loops.append(segments)
    if not loops:
        raise ValueError("The selected region has no sketch profile.")
    return loops


def read_surface(document, region, clock):
    faces, triangle_count, edge_count, seen = [], 0, 0, set()
    references = DB.HostObjectUtils.GetTopFaces(region)
    for reference in references:
        check_time(clock)
        if len(faces) >= MAX_TOP_FACES:
            raise ValueError("The floor region exceeds {} top faces.".format(MAX_TOP_FACES))
        stable = reference.ConvertToStableRepresentation(document)
        if reference.ElementId.Value != region.Id.Value or not stable or stable in seen:
            raise ValueError("The selected region returned a foreign, missing or repeated top face reference.")
        stable = str(stable)
        seen.add(stable)
        face = region.GetGeometryObjectFromReference(reference)
        if not isinstance(face, DB.PlanarFace) or checked_point(face.FaceNormal)[2] <= GEOMETRY_EPSILON_FT:
            raise ValueError("The selected region needs upward-facing planar top faces.")
        for loop in face.EdgeLoops:
            check_time(clock)
            edge_count += loop.Size
            if edge_count > MAX_FACE_EDGES:
                raise ValueError("The floor region exceeds {} top-face edges.".format(MAX_FACE_EDGES))
        mesh = face.Triangulate()
        if mesh is None or mesh.NumTriangles <= 0:
            raise ValueError("A selected top face has no triangulated surface.")
        triangle_count += mesh.NumTriangles
        if triangle_count > MAX_TRIANGLES:
            raise ValueError("The floor region exceeds {} surface triangles.".format(MAX_TRIANGLES))
        triangles = []
        for index in range(mesh.NumTriangles):
            check_time(clock)
            triangle = mesh.get_Triangle(index)
            a, b, c = sorted(checked_point(triangle.get_Vertex(i)) for i in range(3))
            area_twice = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
            if area_twice <= GEOMETRY_EPSILON_FT ** 2:
                raise ValueError("A top-face triangle has a degenerate XY projection.")
            triangles.append([a, b, c])
        faces.append({"reference": stable, "triangles_ft": sorted(triangles)})
    if not faces:
        raise ValueError("No top faces were found on the selected terrain or floor.")
    return sorted(faces, key=lambda face: face["reference"])


def read_region(document, args):
    project = project_identity(document)
    if not isinstance(args, dict) or set(args) != {"region_id", "floor_type_id", "level_id"}:
        raise ValueError("Supply region_id, floor_type_id and level_id.")
    clock = Stopwatch.StartNew()
    region = document.GetElement(element_id(args["region_id"], "Toposolid region"))
    floor_type = document.GetElement(element_id(args["floor_type_id"], "floor type"))
    level = document.GetElement(element_id(args["level_id"], "level"))
    if not isinstance(region, DB.Toposolid):
        raise ValueError("Select a native Toposolid or subdivision.")
    if not isinstance(floor_type, DB.FloorType) or floor_type.IsFoundationSlab:
        raise ValueError("Select a non-foundation floor type.")
    if not isinstance(level, DB.Level) or not finite_number(float(level.ProjectElevation)):
        raise ValueError("Select a level with a valid project elevation.")
    parent = None
    offset = None
    if region.HostTopoId.Value != -1:
        host = document.GetElement(region.HostTopoId)
        if not isinstance(host, DB.Toposolid) or host.Id.Value == region.Id.Value or host.HostTopoId.Value != -1:
            raise ValueError("The selected subdivision has no verifiable host Toposolid.")
        parent = identity(host)
        parameter = region.get_Parameter(DB.BuiltInParameter.TOPOSOLID_SUBDIVIDE_HEIGHT)
        if parameter is None or parameter.StorageType != DB.StorageType.Double or not parameter.HasValue:
            raise ValueError("The subdivision offset is unavailable.")
        offset = float(parameter.AsDouble())
        if not finite_number(offset):
            raise ValueError("The subdivision offset is invalid.")
    sketch = document.GetElement(region.SketchId) if region.SketchId.Value != -1 else None
    if not isinstance(sketch, DB.Sketch) or sketch.OwnerId.Value != region.Id.Value:
        raise ValueError("The selected region has no verifiable sketch of its own.")
    snapshot = {
        "document": project, "region": identity(region), "parent_toposolid": parent,
        "subdivision_offset_ft": offset, "sketch": identity(sketch),
        "floor_type": identity(floor_type), "level": identity(level),
        "level_project_elevation_ft": float(level.ProjectElevation),
        "profile_loops": read_profile(sketch, clock), "top_faces": read_surface(document, region, clock),
    }
    return result_for(snapshot, clock)


def read_floor(document, args):
    project = project_identity(document)
    clock = Stopwatch.StartNew()
    floor = document.GetElement(element_id(args["floor_id"], "floor"))
    if not isinstance(floor, DB.Floor):
        raise ValueError("Select the candidate native floor.")
    floor_type = document.GetElement(floor.GetTypeId())
    level = document.GetElement(floor.LevelId)
    sketch = document.GetElement(floor.SketchId) if floor.SketchId.Value != -1 else None
    if not isinstance(floor_type, DB.FloorType) or floor_type.IsFoundationSlab:
        raise ValueError("The candidate floor needs a non-foundation floor type.")
    if not isinstance(level, DB.Level) or not finite_number(float(level.ProjectElevation)):
        raise ValueError("The candidate floor needs a valid project level.")
    if not isinstance(sketch, DB.Sketch) or sketch.OwnerId.Value != floor.Id.Value:
        raise ValueError("The candidate floor has no verifiable sketch of its own.")
    offset = floor.get_Parameter(DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
    if offset is None or offset.StorageType != DB.StorageType.Double or not offset.HasValue:
        raise ValueError("The candidate floor's level offset is unavailable.")
    offset_ft = float(offset.AsDouble())
    if not finite_number(offset_ft):
        raise ValueError("The candidate floor's level offset is invalid.")
    return result_for({
        "document": project, "floor": identity(floor), "sketch": identity(sketch),
        "floor_type": identity(floor_type), "level": identity(level),
        "level_project_elevation_ft": float(level.ProjectElevation), "floor_offset_ft": offset_ft,
        "profile_loops": read_profile(sketch, clock), "top_faces": read_surface(document, floor, clock),
    }, clock)


def result_for(snapshot, clock):
    payload = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)
    check_time(clock)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("Surface snapshot is {} bytes, above the {} byte limit.".format(
            len(payload), MAX_PAYLOAD_BYTES))
    return {"status": "ok", "snapshot_json": payload}


if "inputs" in globals():
    try:
        if isinstance(inputs, dict) and set(inputs) == {"floor_id"}:
            result = read_floor(doc, inputs)
        else:
            result = read_region(doc, inputs)
    except ValueError as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not read the selected terrain or floor surface.",
                  "error_type": type(error).__name__}
