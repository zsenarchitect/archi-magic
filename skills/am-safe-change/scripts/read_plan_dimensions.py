"""Read host-plan dimension witnesses and the existing linear dimension inventory."""

import hashlib
import json

from pyrevit.api import DB
from System import Int64
from System.Collections.Generic import List

from plan_dimension_preview import (
    ANGLE_TOLERANCE, DISTANCE_TOLERANCE, MAX_INPUT, MAX_WITNESSES,
    check_frame, difference, dot, element_id, label, triple, unit,
)

MAX_EXISTING = 500


def key(id_value):
    return str(id_value.Value)


def get(document, value):
    return document.GetElement(DB.ElementId(Int64(int(element_id(value)))))


def xyz(point):
    return triple([float(point.X), float(point.Y), float(point.Z)])


def identity(element):
    return {"id": element_id(key(element.Id)), "uid": label(str(element.UniqueId))}


def context(document, view, request):
    if (document is None or document.IsFamilyDocument or document.IsLinked or not document.PathName
            or not isinstance(view, DB.ViewPlan) or view.ViewType != DB.ViewType.FloorPlan
            or view.IsTemplate or key(view.GetPrimaryViewId()) != "-1"
            or not document.Equals(view.Document) or key(view.Id) != element_id(request["view_id"])):
        raise ValueError("Open the reviewed independent host floor plan in a saved project.")
    return view


def view_geometry(view):
    sketch = view.SketchPlane
    if sketch is None:
        raise ValueError("Assign a horizontal work plane to the reviewed plan before capturing dimensions.")
    plane = sketch.GetPlane()
    native = [xyz(axis) for axis in (plane.Origin, view.RightDirection, view.UpDirection, view.ViewDirection)]
    origin, axes = check_frame(native)
    axes = [unit(axis)[0] for axis in axes]
    if abs(dot(unit(xyz(plane.Normal))[0], axes[2])) < 1 - ANGLE_TOLERANCE**2 / 2:
        raise ValueError("The work plane does not match the reviewed plan orientation.")
    frame = (origin, axes)
    crop = []
    if view.CropBoxActive:
        for loop in view.GetCropRegionShapeManager().GetCropShape():
            edges = []
            for curve in loop:
                if len(edges) == 128:
                    raise ValueError("The reviewed crop has too many boundary edges.")
                edges.append(line_points(curve, frame))
            crop.append(edges)
            if len(crop) > 8:
                raise ValueError("The reviewed crop has too many regions.")
        if not crop or any(not loop for loop in crop):
            raise ValueError("The active crop boundary is unavailable.")
    return frame, dict(identity(view), frame=native, sketch_plane=identity(sketch), crop_active=bool(view.CropBoxActive),
                       crop=crop)


def on_plane(point, frame):
    origin, axes = frame
    height = dot(difference(point, origin), axes[2])
    return [value - height * normal for value, normal in zip(point, axes[2])]


def line_points(curve, frame):
    if not isinstance(curve, DB.Line) or not curve.IsBound:
        raise ValueError("Revit did not provide a bounded straight curve.")
    return [on_plane(xyz(curve.GetEndPoint(index)), frame) for index in (0, 1)]


def stable(document, element, reference):
    if (reference is None or key(reference.ElementId) != key(element.Id)
            or key(reference.LinkedElementId) != "-1"):
        raise ValueError("The reference does not belong to the exact reviewed host element.")
    return label(str(reference.ConvertToStableRepresentation(document)), 1024)


def witness(document, element, reference, trace):
    return {
        "source_id": key(element.Id), "source_uid": str(element.UniqueId),
        "stable_ref": stable(document, element, reference), "trace": trace,
    }


def grid_witness(document, grid, view, frame):
    if not isinstance(grid, DB.Grid) or grid.IsCurved:
        raise ValueError("Use a straight host grid.")
    first = grid.GetDatumExtentTypeInView(DB.DatumEnds.End0, view)
    second = grid.GetDatumExtentTypeInView(DB.DatumEnds.End1, view)
    if first != second or first not in (DB.DatumExtentType.Model, DB.DatumExtentType.ViewSpecific):
        raise ValueError("The grid has mixed or unresolved view extent modes.")
    curves = list(grid.GetCurvesInView(first, view))
    if len(curves) != 1:
        raise ValueError("The grid does not have one reviewed straight extent.")
    row = witness(document, grid, DB.Reference(grid), line_points(curves[0], frame))
    return dict(row, extent_mode=str(first), reference_form="datum_element")


def face_trace(face, frame, bounded):
    _, axes = frame
    normal, _ = unit(xyz(face.FaceNormal))
    if abs(dot(normal, axes[2])) > ANGLE_TOLERANCE:
        raise ValueError("The witness face is not perpendicular to the view plane.")
    direction, _ = unit([normal[1] * axes[2][2] - normal[2] * axes[2][1],
                         normal[2] * axes[2][0] - normal[0] * axes[2][2],
                         normal[0] * axes[2][1] - normal[1] * axes[2][0]])
    origin = on_plane(xyz(face.Origin), frame)
    if bounded:
        stations = []
        for loop in face.EdgeLoops:
            for edge in loop:
                if len(stations) >= 256:
                    raise ValueError("The wall face boundary exceeds the reviewed edge limit.")
                curve = edge.AsCurve()
                if not isinstance(curve, DB.Line) or not curve.IsBound:
                    raise ValueError("The selected face has a curved or unbounded edge.")
                stations.extend(dot(difference(xyz(curve.GetEndPoint(i)), origin), direction) for i in (0, 1))
        if not stations or max(stations) - min(stations) <= DISTANCE_TOLERANCE:
            raise ValueError("The selected face has no usable plan extent.")
        distances = (min(stations), max(stations))
    else:
        distances = (-1, 1)
    return [[point + distance * axis for point, axis in zip(origin, direction)] for distance in distances], normal


def wall_witness(document, wall, selection, frame):
    if not isinstance(wall, DB.Wall) or not isinstance(wall.Location.Curve, DB.Line):
        raise ValueError("Use a straight host wall.")
    sides = {"interior": DB.ShellLayerType.Interior, "exterior": DB.ShellLayerType.Exterior}
    if selection.get("side") not in sides:
        raise ValueError("Choose the reviewed interior or exterior wall side.")
    candidates = list(DB.HostObjectUtils.GetSideFaces(wall, sides[selection["side"]]))
    requested = selection.get("stable_ref")
    if requested is not None:
        candidates = [item for item in candidates if stable(document, wall, item) == requested]
    if len(candidates) != 1:
        raise ValueError("The wall side does not identify one face. Review its exact stable reference.")
    reference = candidates[0]
    face = wall.GetGeometryObjectFromReference(reference)
    if not isinstance(face, DB.PlanarFace):
        raise ValueError("Revit did not resolve a planar wall face.")
    trace, normal = face_trace(face, frame, True)
    return dict(witness(document, wall, reference, trace), face_normal=normal, side=selection["side"])


def opening_candidate(document, wall, opening):
    categories = {int(DB.BuiltInCategory.OST_Doors), int(DB.BuiltInCategory.OST_Windows)}
    if (not isinstance(wall, DB.Wall) or not isinstance(opening, DB.FamilyInstance)
            or opening.Host is None or key(opening.Host.Id) != key(wall.Id)
            or opening.Category is None or int(opening.Category.Id.Value) not in categories):
        raise ValueError("Choose a door or window hosted by the reviewed wall.")
    refs = []
    for role, kind in (("left", DB.FamilyInstanceReferenceType.Left), ("right", DB.FamilyInstanceReferenceType.Right)):
        matches = list(opening.GetReferences(kind) or [])
        if len(matches) != 1:
            raise ValueError("The opening does not expose one {} reference.".format(role))
        refs.append(matches[0])
    transform = opening.GetTransform()
    candidate = {
        "wall": identity(wall), "opening": identity(opening), "symbol": identity(opening.Symbol),
        "transform": [xyz(point) for point in (transform.Origin, transform.BasisX, transform.BasisY, transform.BasisZ)],
        "mirrored": bool(opening.Mirrored), "hand_flipped": bool(opening.HandFlipped),
        "facing_flipped": bool(opening.FacingFlipped),
        "references": [stable(document, opening, item) for item in refs],
    }
    return candidate, refs


def opening_witnesses(document, opening, candidate, refs, evidence, frame):
    if evidence is not None:
        if not isinstance(evidence, dict) or evidence.get("opening") != candidate:
            raise ValueError("The independently measured opening evidence is stale.")
        traces = evidence.get("traces")
        if not isinstance(traces, list) or len(traces) != 2:
            raise ValueError("Supply both independently measured opening traces.")
        return [witness(document, opening, ref, [triple(point) for point in trace])
                for ref, trace in zip(refs, traces)]

    result = []
    for reference in refs:
        face = opening.GetGeometryObjectFromReference(reference)
        if not isinstance(face, DB.PlanarFace):
            raise ValueError("Revit did not resolve the opening reference plane. Supply independent trace evidence.")
        trace, _ = face_trace(face, frame, False)
        result.append(witness(document, opening, reference, trace))
    return result


def read_dimension(document, dimension, frame):
    if not dimension.AreReferencesAvailable:
        raise ValueError("The linear dimension reference inventory is incomplete.")
    references = []
    for reference in dimension.References:
        if len(references) == MAX_WITNESSES:
            raise ValueError("An existing dimension exceeds the reference limit.")
        owner = document.GetElement(reference.ElementId)
        if owner is None:
            raise ValueError("An existing dimension reference has no resolved owner.")
        references.append(stable(document, owner, reference))
    values = [float(segment.Value) for segment in dimension.Segments] if dimension.NumberOfSegments else [float(dimension.Value)]
    curve = dimension.Curve
    if not isinstance(curve, DB.Line):
        raise ValueError("An existing linear dimension does not expose a straight support line.")
    row = dict(identity(dimension), type_id=key(dimension.GetTypeId()), references=references, segments_ft=values)
    if curve.IsBound:
        row["line"] = line_points(curve, frame)
    else:
        row["support"] = {"point": on_plane(xyz(curve.Origin), frame), "direction": xyz(curve.Direction)}
    return row


def existing_dimensions(document, view, frame):
    result = []
    for dimension in DB.FilteredElementCollector(document, view.Id).OfClass(DB.Dimension):
        if (key(dimension.OwnerViewId) != key(view.Id)
                or dimension.DimensionType.StyleType != DB.DimensionStyleType.Linear):
            continue
        if len(result) == MAX_EXISTING:
            raise ValueError("The existing linear dimension inventory exceeds the reviewed limit.")
        result.append(read_dimension(document, dimension, frame))
    return sorted(result, key=lambda row: int(row["id"]))


def visible_ids(document, view, ids):
    values = List[DB.ElementId]()
    for value in sorted(ids, key=int):
        values.Add(DB.ElementId(Int64(int(value))))
    collector = DB.FilteredElementCollector(document, view.Id).WherePasses(DB.ElementIdSetFilter(values))
    return {key(element.Id) for element in collector if not element.IsHidden(view)}


def source_ids(request):
    ids = request.get("source_ids")
    mode = request.get("mode")
    maximum = MAX_WITNESSES if mode == "grid" else 2
    if (mode not in ("grid", "overall", "opening") or not isinstance(ids, list)
            or not 2 <= len(ids) <= maximum or len(set(map(element_id, ids))) != len(ids)):
        raise ValueError("Use distinct decimal source IDs for the chosen dimension mode.")
    return ids


def read_row(document, view, request, ids, visible, frame):
    row = {"key": label(request["key"]), "mode": request["mode"], "line": request["line"], "source_ids": ids}
    row["unresolved"] = [{"source_id": value, "reason": "Source is unavailable in the reviewed plan."}
                         for value in ids if value not in visible]
    rows = []
    if row["mode"] == "opening":
        if row["unresolved"]:
            return row
        wall, opening = (get(document, value) for value in ids)
        try:
            candidate, references = opening_candidate(document, wall, opening)
            row["opening"] = candidate
            rows = opening_witnesses(document, opening, candidate, references, request.get("opening_evidence"), frame)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            row["unresolved"].append({"source_id": ids[1], "reason": str(error)})
    else:
        selections = request.get("faces")
        if row["mode"] == "overall" and (not isinstance(selections, list) or len(selections) != 2):
            raise ValueError("Supply one reviewed face selection for each wall.")
        for index, value in enumerate(ids):
            if value not in visible:
                continue
            try:
                element = get(document, value)
                if row["mode"] == "grid":
                    rows.append(grid_witness(document, element, view, frame))
                else:
                    rows.append(wall_witness(document, element, selections[index], frame))
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                row["unresolved"].append({"source_id": value, "reason": str(error)})
        if row["mode"] == "overall" and len(rows) == 2 and dot(rows[0]["face_normal"], rows[1]["face_normal"]) > -1 + 1e-10:
            row["unresolved"].append({"source_id": ids[1], "reason": "The reviewed wall faces do not oppose each other."})
    row["witnesses"] = rows
    return row


def read_snapshot(document, view, request):
    context(document, view, request)
    requests = request.get("requests")
    if not isinstance(requests, list) or not 1 <= len(requests) <= 20:
        raise ValueError("Supply 1 to 20 reviewed dimension strings.")
    scopes = [source_ids(item) for item in requests]
    visible = visible_ids(document, view, {value for scope in scopes for value in scope})
    dimension_type = get(document, request["dimension_type_id"])
    if not isinstance(dimension_type, DB.DimensionType) or dimension_type.StyleType != DB.DimensionStyleType.Linear:
        raise ValueError("Select an exact linear dimension type.")
    frame, view_basis = view_geometry(view)
    return {
        "kind": "plan-dimension-snapshot", "version": 1, "request": request,
        "project": {"project_uid": str(document.ProjectInformation.UniqueId),
                    "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()},
        "revit": {"version": str(document.Application.VersionNumber), "build": str(document.Application.VersionBuild)},
        "view": view_basis,
        "dimension_type": identity(dimension_type),
        "short_curve_tolerance_ft": float(document.Application.ShortCurveTolerance),
        "existing": existing_dimensions(document, view, frame), "existing_complete": True,
        "requests": [read_row(document, view, item, ids, visible, frame) for item, ids in zip(requests, scopes)],
    }


def opening_inventory(document, view, request):
    context(document, view, request)
    wall_ids = request.get("opening_wall_ids")
    if (not isinstance(wall_ids, list) or not 1 <= len(wall_ids) <= 20
            or len(set(map(element_id, wall_ids))) != len(wall_ids)):
        raise ValueError("Select 1 to 20 distinct host wall IDs for opening discovery.")
    walls_visible = visible_ids(document, view, set(wall_ids))
    result = []
    for wall_id in wall_ids:
        wall = get(document, wall_id)
        if wall_id not in walls_visible or not isinstance(wall, DB.Wall):
            result.append({"source_id": wall_id, "reason": "Wall is unavailable in the reviewed plan."})
            continue
        inserts = wall.FindInserts(True, False, False, True)
        if inserts.Count > 64:
            raise ValueError("A reviewed wall exceeds the opening inventory limit.")
        ids = {key(item) for item in inserts}
        available = visible_ids(document, view, ids) if ids else set()
        for opening_id in sorted(ids, key=int):
            row = {"wall_id": wall_id, "opening_id": opening_id}
            try:
                if opening_id not in available:
                    raise ValueError("The insert is unavailable in the reviewed plan.")
                candidate, _ = opening_candidate(document, wall, get(document, opening_id))
                row["candidate"] = candidate
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                row["reason"] = str(error)
            result.append(row)
    return {"status": "ok", "view": identity(view), "openings": result}


def main(document, ui_document, args):
    try:
        if ui_document is None or not document.Equals(ui_document.Document):
            raise ValueError("The UI document does not match the reviewed host project.")
        request = args["request"]
        if "opening_wall_ids" in request:
            return opening_inventory(document, ui_document.ActiveView, request)
        result = read_snapshot(document, ui_document.ActiveView, request)
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(encoded) > MAX_INPUT:
            raise ValueError("The complete dimension snapshot exceeds 2 MiB.")
        response = {"status": "ok", "snapshot_json": encoded}
        if "check_absent_ids" in args:
            ids = args["check_absent_ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 20:
                raise ValueError("Use the probe's exact temporary dimension IDs.")
            canonical = sorted({element_id(value) for value in ids}, key=int)
            if ids != canonical or any(get(document, value) is not None for value in ids):
                raise ValueError("The temporary dimension IDs are invalid or still resolve in Revit.")
            response["confirmed_absent_ids"] = canonical
        return response
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        return {"status": "stopped", "message": str(error)}
    except Exception as error:
        return {"status": "stopped", "message": "Revit could not read the dimension evidence.",
                "error_type": type(error).__name__}


if "doc" in globals() and "uidoc" in globals() and "inputs" in globals():
    result = main(doc, uidoc, inputs)
