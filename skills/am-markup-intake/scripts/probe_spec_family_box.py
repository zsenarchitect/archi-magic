# -*- coding: utf-8 -*-
"""Measure a confirmed PDF box extrusion in a disposable Revit family, then roll it back."""

from decimal import Decimal
import hashlib
import json
import math
import platform
import re

from pyrevit.api import DB


TEXT = (str, type(u""))
AXES = ("width", "depth", "height")
TOLERANCE_FT = 1e-7


def snapshot(document):
    if (document is None or not document.IsFamilyDocument or not document.PathName
            or str(document.Application.VersionNumber) != "2026"):
        raise ValueError("Open a saved disposable Revit 2026 family as the active document.")
    if document.IsReadOnly or document.IsWorkshared or document.IsModelInCloud:
        raise ValueError("The fixture must be local, writable and non-workshared.")
    family = document.OwnerFamily
    if (family is None or not family.UniqueId
            or family.FamilyPlacementType != DB.FamilyPlacementType.OneLevelBased
            or family.FamilyCategory is None
            or family.FamilyCategory.Id.Value != DB.ElementId(DB.BuiltInCategory.OST_GenericModel).Value):
        raise ValueError("Use an unhosted, level-based Generic Model fixture.")
    extrusions = DB.FilteredElementCollector(document).OfClass(DB.Extrusion).ToElementIds()
    if extrusions.Count:
        raise ValueError("Use a blank family fixture with no existing extrusions.")
    planes = DB.FilteredElementCollector(document).OfClass(DB.SketchPlane).ToElementIds()
    if planes.Count > 200:
        raise ValueError("The fixture has more than 200 sketch planes.")
    return {"family_uid": str(family.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest(),
            "sketch_plane_ids": sorted(str(item.Value) for item in planes)}


def unique_keys(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("The confirmed specification repeats a JSON key.")
    return result


def specification(text, digest):
    if not isinstance(text, TEXT) or len(text) > 128 * 1024:
        raise ValueError("Supply the confirmed specification JSON text.")
    data = text.encode("utf-8")
    if len(data) > 128 * 1024 or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("The specification bytes differ from the reviewed digest or exceed 128 KiB.")
    spec = json.loads(text, object_pairs_hook=unique_keys)
    if spec["kind"] != "confirmed-family-specification" or spec["model_write_approved"] is not False:
        raise ValueError("Use a confirmed PDF specification. Fixture authorization is separate.")
    values = spec["dimensions_mm"]
    if set(values) != set(AXES):
        raise ValueError("The confirmed specification needs width, depth and height.")
    rows = spec["source_rows"]
    if not isinstance(rows, list) or not 3 <= len(rows) <= 32:
        raise ValueError("The confirmed specification needs 3 to 32 source rows.")
    dimensions = []
    for axis in AXES:
        value = values[axis]
        matching = [row for row in rows if row["name"] == axis]
        if (not isinstance(value, TEXT) or not re.match(r"[0-9]{1,5}(?:\.[0-9]{1,12})?\Z", value)
                or not Decimal(1) <= Decimal(value) <= Decimal(30000)
                or len(matching) != 1 or matching[0]["value"] != value or matching[0]["unit"] != "mm"):
            raise ValueError("The reviewed {} dimension or its source row is invalid.".format(axis))
        dimensions.append(float(Decimal(value) / Decimal("304.8")))
    return dimensions


def measure_box(extrusion):
    bounds = extrusion.get_BoundingBox(None)
    if bounds is None or not bounds.Enabled:
        raise ValueError("Revit did not return the candidate extrusion bounds.")
    corners = [bounds.Transform.OfPoint(DB.XYZ(x, y, z))
               for x in (bounds.Min.X, bounds.Max.X)
               for y in (bounds.Min.Y, bounds.Max.Y)
               for z in (bounds.Min.Z, bounds.Max.Z)]
    axes = [[float(getattr(point, axis)) for point in corners] for axis in ("X", "Y", "Z")]
    if any(math.isnan(value) or math.isinf(value) for values in axes for value in values):
        raise ValueError("The candidate extrusion returned nonfinite geometry.")
    return [min(values) for values in axes], [max(values) for values in axes]


def verify_box(extrusion, dimensions):
    minimum, maximum = measure_box(extrusion)
    if max(abs(value) for value in minimum) > TOLERANCE_FT or any(
            abs(actual - expected) > TOLERANCE_FT for actual, expected in zip(maximum, dimensions)):
        raise ValueError("The Revit extrusion bounds differ from the reviewed box.")
    return {"min_ft": minimum, "max_ft": maximum}


def probe(document, args):
    before = snapshot(document)
    required = {"baseline", "specification_json", "specification_sha256"}
    if not isinstance(args, dict) or set(args) not in (required, required | {"constraint_view"}):
        raise ValueError("Supply the approved family baseline and confirmed specification bytes and digest.")
    if before != args["baseline"]:
        raise ValueError("The active family differs from the reviewed disposable fixture.")
    dimensions = specification(args["specification_json"], args["specification_sha256"])
    if min(dimensions) <= document.Application.ShortCurveTolerance:
        raise ValueError("A reviewed box dimension is below Revit's short-curve limit.")
    view = original_parameters = None
    if "constraint_view" in args:
        from spec_family_box_constraints import add_plan_constraints, family_parameters, plan_view
        view = plan_view(document, args["constraint_view"])
        original_parameters = family_parameters(document)
    width, depth, height = dimensions
    transaction = DB.Transaction(document, "Try reviewed equipment extrusion and roll back")
    created, failure, bounds, flexed = [], None, None, {}
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not confirm the family fixture transaction started.")
        sketch_plane = DB.SketchPlane.Create(document, DB.Plane.CreateByNormalAndOrigin(DB.XYZ.BasisZ, DB.XYZ.Zero))
        created.append(sketch_plane.Id)
        points = [DB.XYZ.Zero, DB.XYZ(width, 0, 0), DB.XYZ(width, depth, 0), DB.XYZ(0, depth, 0)]
        loop = DB.CurveArray()
        for start, end in zip(points, points[1:] + points[:1]):
            loop.Append(DB.Line.CreateBound(start, end))
        profile = DB.CurveArrArray()
        profile.Append(loop)
        extrusion = document.FamilyCreate.NewExtrusion(True, profile, sketch_plane, height)
        created.append(extrusion.Id)
        document.Regenerate()
        bounds = verify_box(extrusion, dimensions)
        if view is not None:
            parameters = add_plan_constraints(document, view, extrusion, dimensions, created)
            verify_box(extrusion, dimensions)
            manager = document.FamilyManager
            for index, (axis, parameter) in enumerate(zip(AXES, parameters)):
                trial = list(dimensions)
                trial[index] *= 1.1
                manager.Set(parameter, trial[index])
                document.Regenerate()
                flexed[axis] = verify_box(extrusion, trial)
                manager.Set(parameter, dimensions[index])
                document.Regenerate()
                verify_box(extrusion, dimensions)
    except ValueError as error:
        failure = {"message": str(error)}
    except Exception as error:
        failure = {"message": "Revit could not create or read the equipment box.", "error_type": type(error).__name__}
    finally:
        try:
            status = transaction.GetStatus()
            if status == DB.TransactionStatus.Started:
                status = transaction.RollBack()
            rolled_back = ((status == DB.TransactionStatus.Uninitialized
                            or status == DB.TransactionStatus.RolledBack == transaction.GetStatus())
                           and all(document.GetElement(element_id) is None for element_id in created)
                           and snapshot(document) == before
                           and (original_parameters is None or family_parameters(document) == original_parameters))
        except Exception:
            rolled_back = False
    if not rolled_back:
        return {"status": "transaction_unresolved", "message": "The original family fixture could not be verified."}
    if failure:
        return dict(failure, status="stopped", rollback_verified=True)
    return {"status": "measured", "rollback_verified": True, "geometry": bounds,
            "reviewed_dimensions_ft": dict(zip(AXES, dimensions)), "flexed_geometry": flexed, "fixture": before,
            "specification_sha256": args["specification_sha256"],
            "revit_build": str(document.Application.VersionBuild), "python_engine": platform.python_implementation()}


if "inputs" in globals():
    try:
        result = {"status": "ok", "baseline": snapshot(doc)} if inputs == {"mode": "read"} else probe(doc, inputs)
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not prepare the family fixture.",
                  "error_type": type(error).__name__}
