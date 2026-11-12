# -*- coding: utf-8 -*-
"""Try the reviewed Toposolid alignment on a disposable model, then roll it back."""

import json
import platform

from System.Diagnostics import Stopwatch
from pyrevit.api import DB

from probe_toposolid_vertex_offset import TOLERANCE_FT, locate, rollback
from read_toposolid_vertices import element_id, project_identity, read_vertices, shape_vertices
from verify_toposolid_alignment import TRANSACTION, digest


def measure_alignment(document, terrain_id, rows, baseline_vertices, tolerance):
    clock = Stopwatch.StartNew()
    expected = {tuple(point[:2]): point for point in baseline_vertices}
    results = []
    for row in rows:
        before, target = row["before_ft"], row["after_ft"]
        key = tuple(before[:2])
        if target[:2] != before[:2] or expected.get(key) != before:
            raise ValueError("The reviewed alignment has an ambiguous or changed vertex.")
        editor, vertices = shape_vertices(document.GetElement(terrain_id), clock)
        vertex, observed = locate(vertices, before)
        if observed != before:
            raise ValueError("The target moved before its alignment step.")
        if abs(before[2] - target[2]) > min(tolerance, TOLERANCE_FT):
            editor.ModifySubElement(vertex, 0.0)
            document.Regenerate()
            editor, vertices = shape_vertices(document.GetElement(terrain_id), clock)
            vertex, origin = locate(vertices, before)
            calibration = dict(expected)
            calibration[key] = origin
            if origin[:2] != before[:2] or sorted(calibration.values()) != sorted(point for _, point in vertices):
                raise ValueError("The offset calibration moved a vertex outside its reviewed XY or scope.")
            editor.ModifySubElement(vertex, float(target[2] - origin[2]))
            document.Regenerate()
        _, vertices = shape_vertices(document.GetElement(terrain_id), clock)
        _, observed = locate(vertices, target)
        residual = abs(observed[2] - target[2])
        if observed[:2] != target[:2] or residual > min(tolerance, TOLERANCE_FT):
            raise ValueError("The native vertex did not reach the reviewed target.")
        expected[key] = observed
        if sorted(expected.values()) != sorted(point for _, point in vertices):
            raise ValueError("Another Toposolid vertex moved during the alignment.")
        results.append({"point": row["point"], "position_ft": observed, "elevation_error_ft": residual})
    return results


def probe(document, args):
    project = project_identity(document)
    if document.IsReadOnly or document.IsWorkshared or document.IsModelInCloud:
        raise ValueError("Open the writable local non-workshared fixture approved for the probe.")
    if not isinstance(args, dict) or set(args) != {"baseline"}:
        raise ValueError("Supply only the reviewed alignment baseline.")
    baseline = args["baseline"]
    if not isinstance(baseline, dict) or baseline.get("document") != project:
        raise ValueError("The open project differs from the reviewed alignment baseline.")
    alignment = baseline["alignment"]
    if alignment["blocked"] or not alignment["rows"]:
        raise ValueError("The reviewed alignment contains blocked or missing rows.")
    points = baseline["vertices_ft"]
    if len({tuple(point[:2]) for point in points}) != len(points):
        raise ValueError("The fixture terrain has stacked or duplicate XY vertices.")
    read_args = {"toposolid_id": baseline["toposolid"]["id"], "alignment": alignment["request"]}
    if json.loads(read_vertices(document, read_args)["snapshot_json"]) != baseline:
        raise ValueError("The terrain, selected support or targets changed since preview.")
    terrain_id = element_id(read_args["toposolid_id"], "Toposolid")
    transaction = DB.Transaction(document, TRANSACTION)
    observed, failure, rolled_back = None, None, False
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not confirm that the alignment transaction started.")
        observed = measure_alignment(document, terrain_id, alignment["rows"], points, alignment["tolerance_ft"])
        current = json.loads(read_vertices(document, read_args)["snapshot_json"])["alignment"]
        if (current["blocked"] or current["support"] != alignment["support"]
                or [row["after_ft"] for row in current["rows"]] != [row["after_ft"] for row in alignment["rows"]]):
            raise ValueError("The selected support or its projected targets changed during alignment.")
        for actual, row in zip(observed, current["rows"]):
            if actual["position_ft"] != row["before_ft"]:
                raise ValueError("The independent alignment read disagrees with the native vertex readback.")
            # The triangle inequality bounds actual 3D separation from the point
            # on the chosen support used by the preview.
            actual["support_distance_bound_ft"] = actual["elevation_error_ft"] + row["support_distance_ft"]
            if actual["support_distance_bound_ft"] > current["tolerance_ft"]:
                raise ValueError("A vertex did not reach the support within the reviewed tolerance.")
    except ValueError as error:
        failure = {"message": str(error)}
    except Exception as error:
        failure = {"message": "Revit could not verify the reviewed alignment.", "error_type": type(error).__name__}
    finally:
        try:
            state = transaction.GetStatus()
            rolled_back = state == DB.TransactionStatus.Uninitialized or rollback(transaction)
        except Exception:
            rolled_back = False

    if rolled_back:
        try:
            rolled_back = json.loads(read_vertices(document, read_args)["snapshot_json"]) == baseline
        except Exception:
            rolled_back = False
    if not rolled_back:
        return {"status": "transaction_unresolved", "message": "Could not verify terrain and support rollback."}
    if failure:
        return dict(failure, status="stopped", rollback_verified=True)
    return {"status": "measured", "rollback_verified": True, "document": project,
            "toposolid": baseline["toposolid"], "support": alignment["support"], "vertices": observed,
            "baseline_sha256": digest(baseline),
            "revit_build": str(document.Application.VersionBuild), "python_engine": platform.python_implementation()}


if "inputs" in globals():
    try:
        result = probe(doc, inputs)
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not prepare the alignment probe.",
                  "error_type": type(error).__name__}
