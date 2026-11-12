# -*- coding: utf-8 -*-
"""Inventory Toposolid vertices and preview alignment through run_query."""

import hashlib
import json
import math
import re

from System import Int64
from System.Diagnostics import Stopwatch
from pyrevit.api import DB


MAX_VERTICES = 2000
MAX_READ_MS = 5000
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_POINTS = 100
GEOMETRY_EPSILON_FT = 1e-8
TEXT = (str, type(u""))


def xyz(point):
    return [float(point.X), float(point.Y), float(point.Z)]


def finite_number(value):
    return type(value) in (int, float) and abs(value) <= 1e9


def edge_target(point, ends, tolerance):
    start, end = ends
    dx, dy = end[0] - start[0], end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= GEOMETRY_EPSILON_FT ** 2:
        raise ValueError("Select a bounded straight edge with a nonvertical XY projection.")
    parameter = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared
    parameter = max(0.0, min(1.0, parameter))
    gap = math.hypot(point[0] - (start[0] + parameter * dx),
                     point[1] - (start[1] + parameter * dy))
    if gap > tolerance:
        return None
    return [point[0], point[1], start[2] + parameter * (end[2] - start[2])], gap


def pad_target(point, face, origin, normal):
    x, y = point[:2]
    z = origin[2] - ((x - origin[0]) * normal[0] + (y - origin[1]) * normal[1]) / normal[2]
    if not finite_number(z):
        return None
    projection = face.Project(DB.XYZ(x, y, z))
    if projection is None or not face.IsInside(projection.UVPoint):
        return None
    projected = xyz(projection.XYZPoint)
    gap = math.sqrt(sum((expected - actual) ** 2 for expected, actual in zip((x, y, z), projected)))
    if not finite_number(gap) or gap > GEOMETRY_EPSILON_FT:
        return None
    return [x, y, z], gap


def alignment_request(document, terrain, request, ui_document):
    if (not isinstance(request, dict) or "tolerance_ft" not in request
            or set(request) - {"points_xy_ft", "support_reference", "tolerance_ft"}):
        raise ValueError("Supply alignment tolerance_ft and optionally points_xy_ft and support_reference.")
    request = dict(request)
    needs_support = "support_reference" not in request
    needs_points = "points_xy_ft" not in request
    if not (needs_support or needs_points):
        return request
    if ui_document is None or not ui_document.Document.Equals(document):
        raise ValueError("Open the active Revit project to read alignment selections.")
    support_refs, picked_xy = [], []
    for index, reference in enumerate(ui_document.Selection.GetReferences()):
        if index >= MAX_POINTS + 1:
            raise ValueError("The alignment selection exceeds {} references.".format(MAX_POINTS + 1))
        if reference.LinkedElementId.Value != -1:
            raise ValueError("Remove linked references from the alignment selection.")
        if reference.ElementId.Value == terrain.Id.Value:
            if needs_points:
                location = reference.GlobalPoint
                if location is None:
                    raise ValueError("Revit returned no selected terrain point coordinates for a reference. "
                                     "Supply points_xy_ft from the inventory.")
                coordinates = xyz(location)
                if not all(finite_number(value) for value in coordinates[:2]):
                    raise ValueError("Revit returned invalid XY for a selected terrain point.")
                picked_xy.append(coordinates[:2])
        elif needs_support:
            stable = reference.ConvertToStableRepresentation(document)
            if not stable:
                raise ValueError("Revit returned no stable identity for a selected support.")
            support_refs.append(stable)
    if needs_support:
        if len(support_refs) != 1:
            raise ValueError("Select exactly one support edge or face in the active project.")
        request["support_reference"] = support_refs[0]
    if needs_points:
        if not picked_xy:
            raise ValueError("Revit returned no selected terrain point coordinates. Supply points_xy_ft from the inventory.")
        request["points_xy_ft"] = sorted(picked_xy)
    return request


def alignment_preview(document, terrain, vertices, request, clock):
    points = request["points_xy_ft"]
    tolerance = request["tolerance_ft"]
    stable = request["support_reference"]
    if not finite_number(tolerance) or tolerance <= 0:
        raise ValueError("Alignment tolerance must be a positive distance in internal feet.")
    if (not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS
            or any(not isinstance(point, list) or len(point) != 2
                   or not all(finite_number(value) for value in point) for point in points)):
        raise ValueError("Supply 1 to {} finite XY point pairs in internal feet.".format(MAX_POINTS))
    if not isinstance(stable, TEXT) or not stable or len(stable) > 4096:
        raise ValueError("Supply one exact stable Revit edge or face reference.")

    reference = DB.Reference.ParseFromStableRepresentation(document, stable)
    if reference.LinkedElementId.Value != -1 or reference.ElementId.Value == terrain.Id.Value:
        raise ValueError("Select a support on a different element in the active project.")
    owner = document.GetElement(reference.ElementId)
    if owner is None or not owner.UniqueId or reference.ConvertToStableRepresentation(document) != stable:
        raise ValueError("The selected support reference could not be resolved exactly.")
    if isinstance(owner, DB.FamilyInstance):
        raise ValueError("Support references inside family instances require model-coordinate qualification.")
    geometry = owner.GetGeometryObjectFromReference(reference)
    if isinstance(geometry, DB.Edge):
        line = geometry.AsCurve()
        if not isinstance(line, DB.Line) or not line.IsBound:
            raise ValueError("Select a bounded straight edge.")
        ends = sorted(xyz(line.GetEndPoint(index)) for index in (0, 1))
        if not all(finite_number(value) for end in ends for value in end):
            raise ValueError("The selected edge has invalid coordinates.")
        support = {"kind": "edge", "ends_ft": ends}

        def target(point):
            return edge_target(point, ends, tolerance)
    elif isinstance(geometry, DB.PlanarFace):
        origin, normal = xyz(geometry.Origin), xyz(geometry.FaceNormal)
        if not all(finite_number(value) for value in origin + normal) or normal[2] <= GEOMETRY_EPSILON_FT:
            raise ValueError("Select an upward-facing planar support face.")
        support = {"kind": "pad_face", "origin_ft": origin, "normal": normal}

        def target(point):
            return pad_target(point, geometry, origin, normal)
    else:
        raise ValueError("Select a bounded straight edge or an upward-facing planar face.")

    support.update({"id": str(owner.Id.Value), "uid": str(owner.UniqueId),
                    "version": str(owner.VersionGuid), "reference": stable})
    rows, seen = [], set()
    for index, (x, y) in enumerate(points, 1):
        if clock.ElapsedMilliseconds >= MAX_READ_MS:
            raise ValueError("Alignment preview exceeded {} ms at point {}.".format(MAX_READ_MS, index))
        matches = [point for point in vertices if math.hypot(point[0] - x, point[1] - y) <= tolerance]
        row = {"point": index, "requested_xy_ft": [x, y]}
        if len(matches) != 1:
            row["reason"] = "ambiguous_vertex" if matches else "missing_vertex"
        elif tuple(matches[0]) in seen:
            row["reason"] = "duplicate_request"
        else:
            point = matches[0]
            seen.add(tuple(point))
            row["before_ft"] = point
            resolved = target(point)
            if resolved is None or resolved[1] > tolerance:
                row["reason"] = "outside_support"
            else:
                row["after_ft"], row["support_distance_ft"] = resolved
        rows.append(row)
    blocked = any("reason" in row for row in rows)
    return {"support": support, "tolerance_ft": tolerance, "blocked": blocked, "rows": rows,
            "request": request}


def project_identity(document):
    if document is None or document.IsFamilyDocument or not document.PathName:
        raise ValueError("Open the saved Revit project containing the Toposolid.")
    if str(document.Application.VersionNumber) != "2026":
        raise ValueError("Use Revit 2026 for this Toposolid inventory.")
    return {"uid": str(document.ProjectInformation.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}


def element_id(value, label):
    if (not isinstance(value, TEXT) or not re.match(r"[1-9][0-9]{0,18}\Z", value)
            or int(value) >= 2 ** 63):
        raise ValueError("Supply a positive decimal {} ID as text.".format(label))
    return DB.ElementId(Int64(int(value)))


def shape_vertices(terrain, clock):
    editor = terrain.GetSlabShapeEditor()
    if editor is None or not editor.IsEnabled:
        raise ValueError("The Toposolid has no enabled slab shape editor.")
    vertices = []
    for vertex in editor.SlabShapeVertices:
        if len(vertices) >= MAX_VERTICES:
            raise ValueError("The Toposolid has more than {} shape vertices.".format(MAX_VERTICES))
        if clock.ElapsedMilliseconds >= MAX_READ_MS:
            raise ValueError("Toposolid read exceeded {} ms after {} vertices.".format(MAX_READ_MS, len(vertices)))
        point = vertex.Position
        coordinates = xyz(point)
        if not all(finite_number(value) for value in coordinates):
            raise ValueError("A Toposolid shape vertex has non-finite or out-of-range coordinates.")
        vertices.append((vertex, coordinates))
    if not vertices:
        raise ValueError("The enabled Toposolid shape editor returned no vertices.")

    return editor, vertices


def read_vertices(document, args, ui_document=None):
    project = project_identity(document)
    if not isinstance(args, dict) or set(args) not in ({"toposolid_id"}, {"toposolid_id", "alignment"}):
        raise ValueError("Supply inputs.toposolid_id and optionally an alignment request.")
    clock = Stopwatch.StartNew()
    terrain = document.GetElement(element_id(args["toposolid_id"], "Toposolid"))
    if not isinstance(terrain, DB.Toposolid) or not terrain.UniqueId:
        raise ValueError("The ID must resolve to a Toposolid in the active project.")

    _, native_vertices = shape_vertices(terrain, clock)
    vertices = [coordinates for _, coordinates in native_vertices]

    snapshot = {
        "document": project,
        "toposolid": {"id": str(terrain.Id.Value), "uid": str(terrain.UniqueId)},
        "vertices_ft": sorted(vertices),
    }
    if "alignment" in args:
        request = alignment_request(document, terrain, args["alignment"], ui_document)
        snapshot["alignment"] = alignment_preview(document, terrain, vertices, request, clock)
    payload = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("Toposolid inventory is {} bytes, above the {} byte limit.".format(
            len(payload), MAX_PAYLOAD_BYTES))
    if clock.ElapsedMilliseconds >= MAX_READ_MS:
        raise ValueError("Toposolid read exceeded {} ms after {} vertices.".format(MAX_READ_MS, len(vertices)))
    return {"status": "ok", "snapshot_json": payload}


if "inputs" in globals():
    try:
        result = read_vertices(doc, inputs, globals().get("uidoc"))
    except ValueError as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not read the Toposolid shape vertices.",
                  "error_type": type(error).__name__}
