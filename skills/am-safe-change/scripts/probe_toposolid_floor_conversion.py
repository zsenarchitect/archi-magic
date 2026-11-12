# -*- coding: utf-8 -*-
"""Try a Toposolid-to-floor conversion on a disposable Revit 2026 model and roll it back."""

import json
import platform

from System.Collections.Generic import List
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

from probe_toposolid_vertex_offset import TOLERANCE_FT, locate, rollback
from read_toposolid_floor_region import read_floor, read_region
from read_toposolid_vertices import element_id, finite_number, project_identity, shape_vertices
from toposolid_floor_shape import MAX_SHAPE_POINTS, MAX_SPLIT_LINES
from toposolid_grade_check import compare_conversion


def profile_at_level(loops, elevation):
    profiles = List[DB.CurveLoop]()
    for segments in loops:
        loop = DB.CurveLoop()
        profiles.Add(loop)
        for segment in segments:
            points = [DB.XYZ(point[0], point[1], elevation) for point in segment["points_ft"]]
            if segment["kind"] == "line":
                curve = DB.Line.CreateBound(points[0], points[-1])
            else:
                curve = DB.Arc.Create(points[0], points[-1], points[1])
            loop.Append(curve)
    return profiles


def check_shape(source, shape):
    if not isinstance(shape, dict) or set(shape) != {"boundary_ft", "interior_ft", "split_lines_xy_ft"}:
        raise ValueError("Supply boundary_ft, interior_ft and split_lines_xy_ft for the floor shape probe.")
    boundary, interior, lines = shape["boundary_ft"], shape["interior_ft"], shape["split_lines_xy_ft"]
    if (not isinstance(boundary, list) or not isinstance(interior, list) or not isinstance(lines, list)
            or len(boundary) + len(interior) > MAX_SHAPE_POINTS or len(lines) > MAX_SPLIT_LINES):
        raise ValueError("The fixture shape exceeds its point or split-line limit.")
    source_vertices = {tuple(point) for face in source["top_faces"]
                       for triangle in face["triangles_ft"] for point in triangle}
    selected_xy = set()
    for point in boundary + interior:
        if (not isinstance(point, list) or len(point) != 3 or not all(finite_number(value) for value in point)
                or tuple(point) not in source_vertices or tuple(point[:2]) in selected_xy):
            raise ValueError("Choose distinct shape vertices from the approved source mesh.")
        selected_xy.add(tuple(point[:2]))
    source_xy = {point[:2] for point in source_vertices}
    seen_lines = set()
    for line in lines:
        if (not isinstance(line, list) or len(line) != 2
                or any(not isinstance(point, list) or len(point) != 2
                       or not all(finite_number(value) for value in point) or tuple(point) not in source_xy
                       for point in line)
                or line[0] == line[1]):
            raise ValueError("Choose two distinct source mesh vertex XY locations for each split line.")
        key = tuple(sorted(tuple(point) for point in line))
        if key in seen_lines:
            raise ValueError("The fixture shape repeats a split line.")
        seen_lines.add(key)


def shape_floor(document, floor, shape):
    boundary, interior, lines = shape["boundary_ft"], shape["interior_ft"], shape["split_lines_xy_ft"]
    if not (boundary or interior or lines):
        return
    editor = floor.GetSlabShapeEditor()
    if editor is None:
        raise ValueError("The candidate floor has no slab shape editor.")
    if not editor.IsEnabled:
        editor.Enable()
        document.Regenerate()
    clock = Stopwatch.StartNew()
    editor, original = shape_vertices(floor, clock)
    # The initial floor is flat. Record each boundary elevation before editing so
    # the probe can test whether the native offset uses that plane.
    base = [locate(original, point)[1][2] for point in boundary]
    for point, base_z in zip(boundary, base):
        editor, vertices = shape_vertices(floor, clock)
        vertex, _ = locate(vertices, point)
        editor.ModifySubElement(vertex, float(point[2] - base_z))
        document.Regenerate()
    if interior:
        editor = floor.GetSlabShapeEditor()
        points = List[DB.XYZ]()
        for point in interior:
            points.Add(DB.XYZ(*point))
        editor.AddPoints(points)
        document.Regenerate()
    for first, second in lines:
        editor, vertices = shape_vertices(floor, clock)
        first_vertex, _ = locate(vertices, first)
        second_vertex, _ = locate(vertices, second)
        editor.AddSplitLine(first_vertex, second_vertex)
        document.Regenerate()
    _, final = shape_vertices(floor, clock)
    for point in boundary + interior:
        _, actual = locate(final, point)
        if abs(actual[2] - point[2]) > TOLERANCE_FT:
            raise ValueError("A shaped floor vertex did not reach the reviewed elevation.")


def probe(document, args):
    project_identity(document)
    if document.IsReadOnly or document.IsWorkshared or document.IsModelInCloud:
        raise ValueError("Open the writable local non-workshared fixture approved for the probe.")
    if not isinstance(args, dict) or set(args) != {
            "baseline", "floor_offset_ft", "xy_tolerance_ft", "area_tolerance_sqft", "elevation_tolerance_ft", "shape"}:
        raise ValueError("Supply the approved source baseline, offset, geometry limits and fixture shape.")
    baseline = args["baseline"]
    request = {"region_id": baseline["region"]["id"], "floor_type_id": baseline["floor_type"]["id"],
               "level_id": baseline["level"]["id"]}
    offset, xy, area, z = (args[name] for name in (
        "floor_offset_ft", "xy_tolerance_ft", "area_tolerance_sqft", "elevation_tolerance_ft"))
    if not all(finite_number(value) for value in (offset, xy, area, z)) or min(xy, area, z) < 0:
        raise ValueError("Supply a finite floor offset and finite nonnegative geometry limits.")
    if json.loads(read_region(document, request)["snapshot_json"]) != baseline:
        raise ValueError("The Toposolid source or destination changed since the fixture baseline.")
    check_shape(baseline, args["shape"])
    floor_type = element_id(request["floor_type_id"], "floor type")
    level = element_id(request["level_id"], "level")
    profiles = profile_at_level(baseline["profile_loops"], baseline["level_project_elevation_ft"])
    if not DB.BoundaryValidation.IsValidHorizontalBoundary(profiles):
        raise ValueError("Revit refused the selected horizontal profile and holes.")

    transaction = DB.Transaction(document, "Try Toposolid floor conversion and roll back")
    floor_id, report, failure, rolled_back = None, None, None, False
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not confirm that the floor probe transaction started.")
        floor = DB.Floor.Create(document, profiles, floor_type, level)
        floor_id = floor.Id
        parameter = floor.get_Parameter(DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        if parameter is None or parameter.IsReadOnly or parameter.StorageType != DB.StorageType.Double:
            raise ValueError("The candidate floor offset is unavailable.")
        if not parameter.Set(float(offset)) and parameter.AsDouble() != offset:
            raise ValueError("Revit refused the candidate floor offset.")
        document.Regenerate()
        shape_floor(document, floor, args["shape"])
        candidate = json.loads(read_floor(document, {"floor_id": str(floor_id.Value)})["snapshot_json"])
        report = compare_conversion(baseline, candidate, area, z, xy, offset)
    except ValueError as error:
        failure = {"message": str(error)}
    except Exception as error:
        failure = {"message": "Revit could not create or inspect the candidate floor.",
                   "error_type": type(error).__name__}
    finally:
        try:
            state = transaction.GetStatus()
            rolled_back = state == DB.TransactionStatus.Uninitialized or rollback(transaction)
        except Exception:
            rolled_back = False

    if rolled_back:
        try:
            rolled_back = ((floor_id is None or document.GetElement(floor_id) is None)
                           and json.loads(read_region(document, request)["snapshot_json"]) == baseline)
        except Exception:
            rolled_back = False
    if not rolled_back:
        return {"status": "transaction_unresolved", "message": "Could not verify source and candidate rollback."}
    if failure:
        return dict(failure, status="stopped", rollback_verified=True)
    return {"status": "measured" if report["matches_floor"] else "mismatch",
            "rollback_verified": True, "revit_build": str(document.Application.VersionBuild),
            "python_engine": platform.python_implementation(), "document": baseline["document"],
            "source_region": baseline["region"], "rolled_back_floor": candidate["floor"], "geometry": report}


if "inputs" in globals():
    try:
        result = probe(doc, inputs)
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not prepare the floor conversion probe.",
                  "error_type": type(error).__name__}
