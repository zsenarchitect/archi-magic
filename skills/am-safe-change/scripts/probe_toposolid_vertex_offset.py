# -*- coding: utf-8 -*-
"""Measure a single Revit 2026 Toposolid vertex offset, with mandatory rollback."""

import math
import platform

from System.Diagnostics import Stopwatch
from pyrevit.api import DB

from read_toposolid_vertices import element_id, finite_number, project_identity, shape_vertices


TOLERANCE_FT = 1e-7


def locate(vertices, target):
    matches = [(vertex, point) for vertex, point in vertices
               if math.hypot(point[0] - target[0], point[1] - target[1]) <= TOLERANCE_FT]
    if len(matches) != 1:
        raise ValueError("The fixture needs one unique vertex at the selected XY location.")
    return matches[0]


def snapshot(project, terrain, vertices):
    return {"document": project, "toposolid": {"id": str(terrain.Id.Value), "uid": str(terrain.UniqueId)},
            "vertices_ft": sorted(point for _, point in vertices)}


def rollback(transaction):
    state = transaction.GetStatus()
    if state == DB.TransactionStatus.Started:
        state = transaction.RollBack()
    return state == DB.TransactionStatus.RolledBack and transaction.GetStatus() == DB.TransactionStatus.RolledBack


def measure(document, args):
    project = project_identity(document)
    if document.IsReadOnly or document.IsWorkshared or document.IsModelInCloud:
        raise ValueError("Open the writable local non-workshared fixture approved for the probe.")
    if not isinstance(args, dict) or set(args) != {"baseline", "vertex_ft", "offsets_ft"}:
        raise ValueError("Supply the approved fixture baseline, vertex_ft and two offsets_ft.")
    baseline, target, offsets = args["baseline"], args["vertex_ft"], args["offsets_ft"]
    if (not isinstance(target, list) or len(target) != 3 or not all(finite_number(value) for value in target)
            or not isinstance(offsets, list) or len(offsets) != 2
            or not all(finite_number(value) for value in offsets)
            or abs(offsets[0] - offsets[1]) <= 10 * TOLERANCE_FT):
        raise ValueError("Supply a finite vertex XYZ and two distinct offsets in internal feet.")
    if not isinstance(baseline, dict) or baseline.get("document") != project:
        raise ValueError("The open project differs from the approved fixture baseline.")
    terrain_id = element_id(baseline["toposolid"]["id"], "Toposolid")
    terrain = document.GetElement(terrain_id)
    if not isinstance(terrain, DB.Toposolid) or not terrain.UniqueId:
        raise ValueError("The fixture Toposolid is unavailable.")
    level = document.GetElement(terrain.LevelId)
    if not isinstance(level, DB.Level):
        raise ValueError("The fixture Toposolid needs an existing level.")
    level_elevation = float(level.ProjectElevation)
    if not finite_number(level_elevation) or abs(level_elevation) <= TOLERANCE_FT:
        raise ValueError("The fixture Toposolid needs a level at nonzero project elevation.")

    clock = Stopwatch.StartNew()
    editor, original = shape_vertices(terrain, clock)
    original_snapshot = snapshot(project, terrain, original)
    if original_snapshot != baseline:
        raise ValueError("The Toposolid vertices or identity changed since the fixture baseline.")
    vertex, original_position = locate(original, target)
    if original_position != target or len(original) < 2:
        raise ValueError("Select the exact recorded vertex and retain at least one sentinel vertex.")
    sentinels = sorted(point for handle, point in original if handle is not vertex)

    transaction = DB.Transaction(document, "Measure Toposolid vertex offset and roll back")
    try:
        started = transaction.Start()
    except Exception as error:
        try:
            if transaction.GetStatus() == DB.TransactionStatus.Uninitialized:
                return {"status": "stopped", "message": "The native transaction did not start.",
                        "error_type": type(error).__name__}
        except Exception:
            pass
        started = None
    if started == DB.TransactionStatus.Uninitialized:
        return {"status": "stopped", "message": "The native transaction did not start."}

    samples, failure = [], None
    try:
        if started != DB.TransactionStatus.Started:
            raise ValueError("Revit did not confirm the probe transaction started.")
        for offset in offsets:
            editor.ModifySubElement(vertex, offset)
            document.Regenerate()
            terrain = document.GetElement(terrain_id)
            editor, current = shape_vertices(terrain, clock)
            vertex, position = locate(current, target)
            if position[:2] != target[:2] or sorted(point for handle, point in current if handle is not vertex) != sentinels:
                raise ValueError("The vertex moved in XY or another vertex changed during the probe.")
            samples.append({"offset_ft": offset, "position_ft": position})
    except ValueError as error:
        failure = {"message": str(error)}
    except Exception as error:
        failure = {"message": "Revit could not measure the vertex offset.", "error_type": type(error).__name__}
    finally:
        try:
            rolled_back = rollback(transaction)
        except Exception:
            rolled_back = False

    if not rolled_back:
        return {"status": "transaction_unresolved", "message": "Revit did not confirm probe rollback."}
    try:
        terrain = document.GetElement(terrain_id)
        _, restored = shape_vertices(terrain, Stopwatch.StartNew())
        unchanged = snapshot(project_identity(document), terrain, restored) == baseline
    except Exception:
        unchanged = False
    if not unchanged:
        return {"status": "transaction_unresolved", "message": "The fixture baseline could not be verified after rollback."}
    if failure:
        return dict(failure, status="stopped", rollback_verified=True)

    first_origin, second_origin = [row["position_ft"][2] - row["offset_ft"] for row in samples]
    existing_offset = target[2] - first_origin
    if abs(first_origin - second_origin) > TOLERANCE_FT or abs(existing_offset) <= TOLERANCE_FT:
        return {"status": "unqualified", "rollback_verified": True, "samples": samples,
                "message": "The samples did not establish one offset origin and a nonzero existing vertex offset."}
    return {"status": "measured", "rollback_verified": True, "document": project,
            "toposolid": baseline["toposolid"], "revit_build": str(document.Application.VersionBuild),
            "python_engine": platform.python_implementation(), "level_project_elevation_ft": level_elevation,
            "baseline_vertex_ft": target, "samples": samples, "measured_offset_origin_ft": first_origin,
            "inferred_existing_offset_ft": existing_offset}


if "inputs" in globals():
    try:
        result = measure(doc, inputs)
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not prepare the vertex offset probe.",
                  "error_type": type(error).__name__}
