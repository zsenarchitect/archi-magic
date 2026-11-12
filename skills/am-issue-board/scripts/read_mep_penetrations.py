"""Read bounded linked MEP crossing evidence from Revit without changing the model."""

from itertools import product
import json
import math
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from issue_board import saved_document_identity
from mep_penetration_preview import fingerprint
from mep_penetration_spacing import profile, scalar, spacing_conflicts, vertices
from mep_penetration_tracking import read_tracking
from structural_compare_cli import LIMIT, bounded_list, checked_id, identity, uid


class ReadLimit(ValueError):
    pass


class ReadBudget:
    def __init__(self, seconds=10):
        self.deadline = monotonic() + seconds
        self.visits = 0
        self.intersections = 0

    def visit(self, count=1):
        self.visits += count
        if self.visits > 100000 or monotonic() >= self.deadline:
            raise ReadLimit("Geometry exceeded the read budget. Review a smaller scope.")

    def intersect(self):
        self.intersections += 1
        if self.intersections > 1000:
            raise ReadLimit("The scope exceeded 1,000 solid-curve calls.")
        self.visit()


def xyz(point):
    values = [scalar(value) for value in (point.X, point.Y, point.Z)]
    if any(abs(value) > 1e6 for value in values):
        raise ValueError("The geometry lies outside the supported coordinate range.")
    return values


def element_record(element):
    return {"id": checked_id(str(element.Id.Value)), "uid": uid(str(element.UniqueId))}


def selected(document, DB, values):
    identifiers = [checked_id(value) for value in bounded_list(values, 100)]
    if not identifiers or len(set(identifiers)) != len(identifiers):
        raise ValueError("Select between one and one hundred distinct element IDs per model.")
    from System import Int64

    elements = [document.GetElement(DB.ElementId(Int64(int(value)))) for value in identifiers]
    if any(element is None for element in elements):
        raise ValueError("At least one reviewed element is unavailable.")
    return elements


def qualified_transform(link, tolerance):
    transform = link.GetTransform()
    total = link.GetTotalTransform()
    if (not transform.IsConformal or transform.HasReflection or abs(transform.Scale - 1.0) > 1e-9
            or transform.Origin.DistanceTo(total.Origin) > tolerance
            or any(getattr(transform, key).DistanceTo(getattr(total, key)) > 1e-9
                   for key in ("BasisX", "BasisY", "BasisZ"))):
        raise ValueError("The linked coordinate transform is reflected, scaled, or ambiguous.")
    return transform


def bounds(DB, element, transform):
    box = element.get_BoundingBox(None)
    if box is None:
        raise ValueError("An element has no conservative model bounds.")
    corners = [xyz(transform.OfPoint(box.Transform.OfPoint(DB.XYZ(x, y, z))))
               for x, y, z in product((box.Min.X, box.Max.X), (box.Min.Y, box.Max.Y), (box.Min.Z, box.Max.Z))]
    return [(min(axis), max(axis)) for axis in zip(*corners)]


def native_solids(DB, element, budget):
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    options.IncludeNonVisibleObjects = False
    budget.visit()
    items = element.get_Geometry(options)
    budget.visit()
    solids = []
    for item in items or ():
        budget.visit()
        if isinstance(item, (DB.GeometryInstance, DB.Mesh)):
            raise ValueError("Nested or mesh geometry cannot prove the selected solid.")
        if isinstance(item, DB.Solid) and item.Faces.Size and item.Volume > 0:
            solids.append(item)
    if not solids:
        raise ValueError("The element has no measured positive-volume solid.")
    return solids


def face_uv(face, point):
    relative = point.Subtract(face.Origin)
    return [relative.DotProduct(face.XVector), relative.DotProduct(face.YVector)]


def loops(DB, face, budget):
    result = []
    for loop in face.EdgeLoops:
        curves = []
        for edge in loop:
            budget.visit()
            curves.append(edge.AsCurve())
        if not curves:
            raise ValueError("The host face contains an empty trim loop.")
        result.append(curves)
    return result


def rectangle_edges(DB, curves, tolerance):
    if len(curves) != 4 or not all(isinstance(curve, DB.Line) and curve.IsBound for curve in curves):
        raise ValueError("The host outline is outside the supported rectangular scope.")
    edges = [(curve.GetEndPoint(0), curve.GetEndPoint(1)) for curve in curves]
    for index, (a, b) in enumerate(edges):
        if a.DistanceTo(b) <= tolerance:
            raise ValueError("The measured host outline has a degenerate edge.")
        for vertex, other_end in ((a, b), (b, a)):
            neighbors = [end.Subtract(vertex) for other_index, edge in enumerate(edges) if other_index != index
                         for near, end in (edge, edge[::-1]) if near.DistanceTo(vertex) <= tolerance]
            direction = other_end.Subtract(vertex).Normalize()
            if len(neighbors) != 1 or abs(direction.DotProduct(neighbors[0].Normalize())) > 1e-9:
                raise ValueError("The measured host outline has an ambiguous corner.")
    return edges


def loop_profile(DB, curves, frame, tolerance):
    if all(isinstance(curve, DB.Arc) for curve in curves):
        first = curves[0]
        radius = scalar(first.Radius)
        length = sum(curve.ApproximateLength for curve in curves)
        if (radius <= tolerance or abs(length - 2 * math.pi * radius) > tolerance
                or any(abs(curve.Radius - radius) > tolerance or curve.Center.DistanceTo(first.Center) > tolerance
                       or abs(curve.Normal.DotProduct(frame.Normal)) < 1 - 1e-9 for curve in curves)):
            raise ValueError("A host trim does not form a supported circular cut.")
        return {"shape": "round", "center": face_uv(frame, first.Center), "diameter": 2 * radius}, None

    edges = rectangle_edges(DB, curves, tolerance)
    points = sorted(face_uv(frame, point) for edge in edges for point in edge)
    a, b = sorted(sorted(face_uv(frame, point) for point in edge) for edge in edges)[0]
    center = [(a + b) / 2 for a, b in zip(points[0], points[-1])]
    width = math.dist(a, b)
    height = next(math.dist(*edge) for edge in ([face_uv(frame, point) for point in edge] for edge in edges)
                  if any(math.dist(a, point) <= tolerance for point in edge)
                  and not any(math.dist(b, point) <= tolerance for point in edge))
    angle = math.remainder(math.atan2(b[1] - a[1], b[0] - a[0]), math.pi)
    return {"shape": "rectangular", "center": center, "width": width, "height": height,
            "angle_rad": angle}, edges


def area(outline):
    if outline["shape"] == "round":
        return math.pi * (outline["diameter"] / 2) ** 2
    return outline["width"] * outline["height"]


def margin(outline, point):
    center, size = profile(outline)
    dx, dy = point[0] - center[0], point[1] - center[1]
    if outline["shape"] == "round":
        return size - math.hypot(dx, dy)
    width, height, cos, sin = size
    return min(width - abs(dx * cos + dy * sin), height - abs(-dx * sin + dy * cos))


def matches_outline(first, second, tolerance):
    if first["shape"] != second["shape"] or math.dist(first["center"], second["center"]) > tolerance:
        return False
    if first["shape"] == "round":
        return abs(first["diameter"] - second["diameter"]) <= tolerance
    a = vertices(*profile(first))
    b = vertices(*profile(second))
    return all(sum(math.dist(point, other) <= tolerance for other in b) == 1 for point in a)


def host_face_profiles(DB, face_loops, frame, tolerance, budget):
    profiles = [loop_profile(DB, curves, frame, tolerance) for curves in face_loops]
    if not profiles:
        raise ValueError("The host has no complete face boundaries.")
    profiles.sort(key=lambda row: area(row[0]), reverse=True)
    outer, edges = profiles[0]
    if edges is None:
        raise ValueError("The host outer outline is not rectangular.")
    holes = [outline for outline, _ in profiles[1:]]
    for hole in holes:
        center, size = profile(hole)
        if hole["shape"] == "round":
            fits = margin(outer, center) > size + tolerance
        else:
            fits = all(margin(outer, point) > tolerance for point in vertices(center, size))
        if not fits:
            raise ValueError("An existing cut reaches the outer host boundary.")
    keyed = [dict(hole, key=str(index)) for index, hole in enumerate(holes)]
    budget.visit(len(keyed) * (len(keyed) - 1) // 2)
    if spacing_conflicts(keyed, [], tolerance):
        raise ValueError("Existing cuts overlap or cannot be measured independently.")
    budget.visit(0)
    return outer, edges, holes


def world_point(frame, uv, depth):
    return frame.Origin.Add(frame.XVector.Multiply(uv[0])).Add(frame.YVector.Multiply(uv[1])).Add(
        frame.Normal.Multiply(depth))


def verify_prism(DB, solid, faces, frame, depth, outer, holes, tolerance, budget):
    rectangles = [outer] + [hole for hole in holes if hole["shape"] == "rectangular"]
    circles = [hole for hole in holes if hole["shape"] == "round"]
    side_corners = []
    for rectangle in rectangles:
        corners = vertices(*profile(rectangle))
        for a, b in zip(corners, corners[1:] + corners[:1]):
            side_corners.append([world_point(frame, point, z) for point in (a, b) for z in (0, depth)])
    cylindrical_areas = [0.0] * len(circles)
    principal = 0
    for face in solid.Faces:
        budget.visit()
        if isinstance(face, DB.PlanarFace):
            if abs(face.FaceNormal.DotProduct(frame.Normal)) >= 1 - 1e-9:
                station = face.Origin.Subtract(frame.Origin).DotProduct(frame.Normal)
                if min(abs(station), abs(station - depth)) > tolerance:
                    raise ValueError("The host has an internal or stepped planar face.")
                principal += 1
                continue
            boundaries = loops(DB, face, budget)
            if len(boundaries) != 1:
                raise ValueError("The host side face has an unsupported trim.")
            actual = [point for edge in rectangle_edges(DB, boundaries[0], tolerance) for point in edge]
            budget.visit(len(side_corners))
            candidates = [index for index, expected in enumerate(side_corners)
                          if all(any(point.DistanceTo(corner) <= tolerance for point in actual) for corner in expected)
                          and all(any(point.DistanceTo(corner) <= tolerance for corner in expected) for point in actual)]
            if len(candidates) != 1:
                raise ValueError("The host side does not match one measured through profile.")
            side_corners.pop(candidates[0])
        elif isinstance(face, DB.CylindricalFace):
            if abs(face.Axis.DotProduct(frame.Normal)) < 1 - 1e-9:
                raise ValueError("A circular cut is not perpendicular to the host.")
            center = face_uv(frame, face.Origin)
            radius = face.get_Radius(0).GetLength()
            budget.visit(len(circles))
            candidates = [index for index, circle in enumerate(circles) if math.dist(center, circle["center"]) <= tolerance
                          and abs(2 * radius - circle["diameter"]) <= tolerance]
            if len(candidates) != 1 or abs(face.get_Radius(1).GetLength() - radius) > tolerance:
                raise ValueError("The cylindrical side does not match one measured through cut.")
            points = []
            for loop in loops(DB, face, budget):
                for curve in loop:
                    if isinstance(curve, DB.Arc):
                        points.append(curve.Center)
                    elif isinstance(curve, DB.Line) and curve.IsBound:
                        points.extend(curve.GetEndPoint(index) for index in (0, 1))
                    else:
                        raise ValueError("The cylindrical side has an unsupported trim.")
            stations = [point.Subtract(frame.Origin).DotProduct(frame.Normal) for point in points]
            if not stations or abs(min(stations)) > tolerance or abs(max(stations) - depth) > tolerance:
                raise ValueError("The cylindrical side does not span the full host depth.")
            cylindrical_areas[candidates[0]] += face.Area
        else:
            raise ValueError("The host has an unsupported solid face.")
    if (principal != len(faces) or side_corners or
            any(abs(actual - math.pi * cut["diameter"] * depth) > tolerance * (depth + cut["diameter"])
                for actual, cut in zip(cylindrical_areas, circles))):
        raise ValueError("The host solid does not match the complete through-cut inventory.")


def element_scope(element):
    option = element.DesignOption
    if option is not None or element.DemolishedPhaseId.Value != -1:
        raise ValueError("The element belongs to a design option or has a demolition phase.")
    return {"version_guid": str(element.VersionGuid), "created_phase_id": str(element.CreatedPhaseId.Value)}


def principal_references(DB, element):
    if isinstance(element, DB.Wall) and element.WallType.Kind == DB.WallKind.Basic:
        kind = "wall"
        refs = [DB.HostObjectUtils.GetSideFaces(element, side)
                for side in (DB.ShellLayerType.Exterior, DB.ShellLayerType.Interior)]
    elif isinstance(element, DB.Floor):
        kind = "floor"
        refs = [DB.HostObjectUtils.GetBottomFaces(element), DB.HostObjectUtils.GetTopFaces(element)]
    else:
        raise ValueError("Use planar basic walls or flat floors.")
    if any(len(rows) != 1 for rows in refs):
        raise ValueError("The host does not expose two unambiguous principal faces.")
    return kind, [list(rows)[0] for rows in refs]


def host_fire_rating(DB, element):
    parameter_id = DB.BuiltInParameter.FIRE_RATING
    element_type = element.Document.GetElement(element.GetTypeId())
    for source in (element, element_type):
        if source is None:
            continue
        parameter = source.get_Parameter(parameter_id)
        if parameter is not None and parameter.StorageType == DB.StorageType.String:
            value = parameter.AsString()
            if value and str(value).strip():
                return str(value).strip()
    return None


def principal_host(DB, element, minimum_web, tolerance, budget):
    scope = element_scope(element)
    kind, refs = principal_references(DB, element)
    faces = [element.GetGeometryObjectFromReference(reference) for reference in refs]
    if not all(isinstance(face, DB.PlanarFace) for face in faces):
        raise ValueError("The host principal faces are not planar.")
    first, second = faces
    normal = first.FaceNormal.Negate()
    depth = second.Origin.Subtract(first.Origin).DotProduct(normal)
    if (depth <= tolerance or normal.DotProduct(second.FaceNormal) < 1 - 1e-9
            or (kind == "wall" and abs(normal.Z) > 1e-9) or (kind == "floor" and abs(normal.Z) < 1 - 1e-9)):
        raise ValueError("The host faces are sloped, nonparallel, or have no verified thickness.")

    first_loops, second_loops = [loops(DB, face, budget) for face in faces]
    initial = SimpleNamespace(Origin=first.Origin, XVector=first.XVector, YVector=first.YVector, Normal=normal)
    _, outer_edges, _ = host_face_profiles(DB, first_loops, initial, tolerance, budget)
    origin, neighbor = min(((point, other) for edge in outer_edges for point, other in (edge, edge[::-1])),
                           key=lambda pair: (xyz(pair[0]), xyz(pair[1])))
    x = neighbor.Subtract(origin).Normalize()
    frame = SimpleNamespace(Origin=origin, XVector=x, YVector=normal.CrossProduct(x), Normal=normal)
    outer, outer_edges, holes = host_face_profiles(DB, first_loops, frame, tolerance, budget)
    other_outer, other_edges, other_holes = host_face_profiles(DB, second_loops, frame, tolerance, budget)
    if not matches_outline(outer, other_outer, tolerance) or len(holes) != len(other_holes):
        raise ValueError("The host face outlines differ across its depth.")
    for hole in holes:
        budget.visit(len(other_holes))
        matches = [index for index, other in enumerate(other_holes) if matches_outline(hole, other, tolerance)]
        if len(matches) != 1:
            raise ValueError("A host cut cannot be matched uniquely across its depth.")
        other_holes.pop(matches[0])

    solids = native_solids(DB, element, budget)
    net_area = area(outer) - sum(area(hole) for hole in holes)
    if (len(solids) != 1 or net_area <= 0 or abs(solids[0].Volume - net_area * depth) > tolerance * net_area
            or any(abs(face.Area - net_area) > tolerance * max(outer["width"], outer["height"]) for face in faces)):
        raise ValueError("The host area and volume do not match the measured profiles.")
    verify_prism(DB, solids[0], faces, frame, depth, outer, holes, tolerance, budget)

    corners = sorted(xyz(point) for outline in (outer_edges, other_edges) for edge in outline for point in edge)
    holes.sort(key=lambda row: (row["center"], row["shape"]))
    existing = [dict(hole, key=f"measured-cut-{index + 1}") for index, hole in enumerate(holes)]
    row = dict(element_record(element), kind=kind, normal=xyz(normal), depth_ft=scalar(depth),
               fire_rating=host_fire_rating(DB, element),
               gross_sha256=fingerprint(corners), cuts_complete=True, minimum_web_ft=minimum_web, existing=existing,
               **scope)
    return row, (faces, frame, outer, holes, solids[0], bounds(DB, element, DB.Transform.Identity))


def physical_ends(DB, element, curve, tolerance, budget):
    connectors = []
    for connector in element.ConnectorManager.Connectors:
        budget.visit()
        if connector.ConnectorType == DB.ConnectorType.End:
            connectors.append(connector)
    if len(connectors) != 2:
        raise ValueError("The run does not have exactly two physical end connectors.")
    ends = [curve.GetEndPoint(index) for index in (0, 1)]
    connectors.sort(key=lambda connector: connector.Origin.DistanceTo(ends[0]))
    axis = ends[1].Subtract(ends[0]).Normalize()
    if any(connector.Origin.DistanceTo(point) > tolerance or
           abs(connector.CoordinateSystem.BasisZ.DotProduct(axis)) < 1 - 1e-9
           for connector, point in zip(connectors, ends)):
        raise ValueError("The physical connectors do not establish the exact run axis.")
    return connectors, ends, axis


def measured_profile(DB, element, kind, connectors, axis, tolerance, budget):
    first, second = connectors
    if first.Shape != second.Shape:
        raise ValueError("The run changes section shape.")
    if first.Shape == DB.ConnectorProfileType.Round:
        cylinders = []
        for solid in native_solids(DB, element, budget):
            for face in solid.Faces:
                budget.visit()
                if isinstance(face, DB.CylindricalFace):
                    if abs(face.Axis.DotProduct(axis)) < 1 - 1e-9:
                        raise ValueError("The measured cylinder does not follow the reviewed run axis.")
                    relative = face.Origin.Subtract(first.Origin)
                    offset = relative.Subtract(axis.Multiply(relative.DotProduct(axis))).GetLength()
                    radius = scalar(face.get_Radius(0).GetLength())
                    if offset > tolerance or abs(face.get_Radius(1).GetLength() - radius) > tolerance:
                        raise ValueError("The measured circular profile is off-axis or unsupported.")
                    cylinders.append(radius)
                elif not isinstance(face, DB.PlanarFace) or abs(face.FaceNormal.DotProduct(axis)) < 1 - 1e-9:
                    raise ValueError("The round run contains a face outside the measured cylindrical profile.")
        if not cylinders:
            raise ValueError("The circular run has no measured exterior cylinder.")
        diameter = 2 * max(cylinders)
        if kind == "pipe":
            outer = element.get_Parameter(DB.BuiltInParameter.RBS_PIPE_OUTER_DIAMETER)
            if outer is None or not outer.HasValue or abs(outer.AsDouble() - diameter) > tolerance:
                raise ValueError("The pipe outside diameter differs from its measured geometry.")
        if diameter <= tolerance:
            raise ValueError("The exterior diameter is not usable.")
        return {"shape": "round", "diameter": diameter}, None

    if kind != "duct" or first.Shape != DB.ConnectorProfileType.Rectangular:
        raise ValueError("The exterior profile is unsupported.")
    u = first.CoordinateSystem.BasisX
    v = first.CoordinateSystem.BasisY
    if (abs(u.DotProduct(second.CoordinateSystem.BasisX)) < 1 - 1e-9
            or abs(v.DotProduct(second.CoordinateSystem.BasisY)) < 1 - 1e-9
            or abs(first.Width - second.Width) > tolerance or abs(first.Height - second.Height) > tolerance):
        raise ValueError("The rectangular run changes size or section orientation.")
    sizes = [[], []]
    for solid in native_solids(DB, element, budget):
        for edge in solid.Edges:
            budget.visit()
            curve = edge.AsCurve()
            if not isinstance(curve, DB.Line) or not curve.IsBound:
                raise ValueError("The rectangular duct has an unsupported exterior edge.")
            for index in (0, 1):
                point = curve.GetEndPoint(index).Subtract(first.Origin)
                for values, direction in zip(sizes, (u, v)):
                    values.append(point.DotProduct(direction))
    if not all(sizes):
        raise ValueError("The rectangular duct has no exterior measurements.")
    width, height = [max(values) - min(values) for values in sizes]
    if (min(width, height) <= tolerance or abs(first.Width - width) > tolerance
            or abs(first.Height - height) > tolerance or any(abs(max(values) + min(values)) > tolerance for values in sizes)):
        raise ValueError("The rectangular connector sizes differ from the measured exterior.")
    return {"shape": "rectangular", "width": width, "height": height}, u


def measured_run(DB, document, element, transform, tolerance, budget):
    scope = element_scope(element)
    if isinstance(element, DB.Mechanical.Duct):
        kind = "duct"
    elif isinstance(element, DB.Plumbing.Pipe):
        kind = "pipe"
    else:
        raise ValueError("Use a rigid duct or pipe.")
    if (len(DB.InsulationLiningBase.GetInsulationIds(document, element.Id))
            or (kind == "duct" and len(DB.InsulationLiningBase.GetLiningIds(document, element.Id)))):
        raise ValueError("The installed insulation or lining envelope is unqualified.")
    curve = element.Location.Curve if isinstance(element.Location, DB.LocationCurve) else None
    if not isinstance(curve, DB.Line) or not curve.IsBound or curve.Length <= tolerance:
        raise ValueError("The run has no supported finite straight centerline.")
    connectors, ends, axis = physical_ends(DB, element, curve, tolerance, budget)
    profile, width_direction = measured_profile(DB, element, kind, connectors, axis, tolerance, budget)
    host_ends = [transform.OfPoint(point) for point in ends]
    line = DB.Line.CreateBound(*host_ends)
    system = str(element.MEPSystem.Name)[:160] if element.MEPSystem is not None else None
    row = dict(element_record(element), profile=profile, system=system, **scope)
    width_axis = transform.OfVector(width_direction) if width_direction is not None else None
    return row, (line, host_ends, width_axis, bounds(DB, element, transform))


def measured_pair(DB, host, run, clearance, tolerance, budget):
    faces, frame, outer, holes, solid, host_box = host
    line, ends, width_axis, run_box = run
    padding = clearance + tolerance
    if any(low > r_high + padding or r_low - padding > high
           for (low, high), (r_low, r_high) in zip(host_box, run_box)):
        return {"status": "no_crossing", "gross_verified": True}

    normal = frame.Normal
    first, second = ends
    delta = second.Subtract(first)
    along = delta.DotProduct(normal)
    if abs(along) <= tolerance or delta.Subtract(normal.Multiply(along)).GetLength() > tolerance:
        raise ValueError("The run is not perpendicular to the host faces.")
    start = first.Subtract(frame.Origin).DotProduct(normal)
    depth = faces[1].Origin.Subtract(frame.Origin).DotProduct(normal)
    if min(start, start + along) >= -tolerance or max(start, start + along) <= depth + tolerance:
        raise ValueError("The finite run does not extend past both faces.")
    points = [first.Add(delta.Multiply((station - start) / along)) for station in (0, depth)]
    centers = [face_uv(frame, point) for point in points]
    edge_distance = min(margin(outer, center) for center in centers)
    if edge_distance <= tolerance:
        raise ValueError("The crossing coincides with or lies outside the outer host edge.")

    options = DB.SolidCurveIntersectionOptions()
    intersection = None
    try:
        budget.intersect()
        intersection = solid.IntersectWithCurve(line, options)
        budget.visit()
        if intersection.SegmentCount == 0:
            covering = [hole for hole in holes if all(margin(hole, center) > tolerance for center in centers)]
            if len(covering) != 1:
                raise ValueError("The measured gross crossing has no uniquely verified existing cut.")
            status = "gross_crossing"
        elif intersection.SegmentCount == 1:
            segment = intersection.GetCurveSegment(0)
            measured = sorted((segment.GetEndPoint(0), segment.GetEndPoint(1)),
                              key=lambda point: point.Subtract(frame.Origin).DotProduct(normal))
            if any(a.DistanceTo(b) > tolerance for a, b in zip(measured, points)):
                raise ValueError("The solid crossing does not span both principal faces.")
            for face, point in zip(faces, points):
                budget.visit()
                projected = face.Project(point)
                if projected is None or projected.Distance > tolerance or not face.IsInside(projected.UVPoint):
                    raise ValueError("The crossing does not meet both trimmed face interiors.")
            status = "solid_crossing"
        else:
            raise ValueError("The solid produced multiple crossing segments.")
    finally:
        if intersection is not None:
            intersection.Dispose()
        options.Dispose()

    row = {"status": status, "entry_ft": xyz(points[0]), "exit_ft": xyz(points[1]),
           "center_uv_ft": centers[0], "edge_distance_ft": edge_distance, "gross_verified": True}
    if status == "gross_crossing":
        row["existing_cut_verified"] = True
    if width_axis is not None:
        row["angle_rad"] = math.atan2(width_axis.DotProduct(frame.YVector), width_axis.DotProduct(frame.XVector))
    return row


def inventory(elements, measure, budget):
    records = []
    for element in elements:
        budget.visit()
        try:
            row, geometry = measure(element)
        except ReadLimit:
            raise
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            row, geometry = dict(element_record(element), reason=reason, cuts_complete=False), None
        records.append((row, geometry))
    return records


def project_inventory(document, DB, budget):
    inventory = {}
    for collector in (DB.FilteredElementCollector(document).WhereElementIsElementType(),
                      DB.FilteredElementCollector(document).WhereElementIsNotElementType()):
        for element in collector:
            budget.visit()
            key = str(element.Id.Value)
            if key in inventory or len(inventory) >= 10000:
                raise ValueError("The disposable project exceeds the complete inventory limit.")
            inventory[key] = (str(element.UniqueId), str(element.VersionGuid))
    return inventory


def read(DB, document, args):
    audit = args.get("audit")
    kept = audit is not None and audit.get("kept") is True and bool(audit.get("trial_transaction"))
    if document is None or document.IsFamilyDocument or document.IsModifiable or (document.IsModified and not kept):
        raise ValueError("Open the saved Revit project in read-only query mode.")
    project = saved_document_identity(document)
    link, = selected(document, DB, [args["link_instance_id"]])
    if not isinstance(link, DB.RevitLinkInstance):
        raise ValueError("Select an exact loaded Revit link instance.")
    linked = link.GetLinkDocument()
    if linked is None or linked.IsModified:
        raise ValueError("The selected Revit link is unloaded or has unsaved changes.")
    linked_identity = saved_document_identity(linked)
    clearance = scalar(args["clearance_ft"])
    tolerance = scalar(args["tolerance_ft"])
    minimum_web = scalar(args["minimum_web_ft"])
    if clearance < 0 or tolerance <= 0 or minimum_web <= 0:
        raise ValueError("Use nonnegative clearance, positive tolerance, and positive minimum web.")
    transform = qualified_transform(link, tolerance)
    budget = ReadBudget()
    hosts = inventory(selected(document, DB, args["host_ids"]),
                      lambda element: principal_host(DB, element, minimum_web, tolerance, budget), budget)
    runs = inventory(selected(linked, DB, args["run_ids"]),
                     lambda element: measured_run(DB, linked, element, transform, tolerance, budget), budget)
    symbols = []
    if args.get("symbol_ids"):
        for symbol in selected(document, DB, args["symbol_ids"]):
            budget.visit()
            if not isinstance(symbol, DB.FamilySymbol):
                raise ValueError("An opening type ID does not identify a family symbol.")
            symbols.append(dict(element_record(symbol), version_guid=str(symbol.VersionGuid),
                                is_active=bool(symbol.IsActive),
                                placement_type=str(symbol.Family.FamilyPlacementType)))
    pairs = []
    for (host, host_geometry), (run, run_geometry) in product(hosts, runs):
        budget.visit()
        try:
            if host_geometry is None or run_geometry is None:
                raise ValueError(host.get("reason") or run.get("reason") or "The pair has incomplete geometry.")
            row = measured_pair(DB, host_geometry, run_geometry, clearance, tolerance, budget)
        except ReadLimit:
            raise
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            row = {"status": "unresolved", "reason": reason}
        pairs.append(dict(row, host_uid=host["uid"], run_uid=run["uid"]))
    link_uid = uid(str(link.UniqueId))
    tracking_hosts = {row["uid"]: (row, geometry[1] if geometry is not None else None) for row, geometry in hosts}
    tracked, tracking_issues = read_tracking(DB, document, tracking_hosts, identity(project), identity(linked_identity),
                                            link_uid, tolerance, budget)
    if (document.IsModified and not kept) or linked.IsModified or document.IsModifiable:
        raise ValueError("The host or linked document changed during the read.")
    report = {"kind": "mep-penetration-evidence", "frame": "revit_host_internal_feet",
              "host_identity": project, "link_identity": linked_identity, "link_instance_uid": link_uid,
              "link_instance_id": checked_id(str(link.Id.Value)),
              "link_transform": {key: xyz(getattr(transform, key)) for key in ("Origin", "BasisX", "BasisY", "BasisZ")},
              "clearance_ft": clearance, "tolerance_ft": tolerance, "complete": not tracking_issues,
              "hosts": [row for row, _ in hosts], "runs": [row for row, _ in runs], "pairs": pairs, "tracked": tracked,
              "tracking_issues": tracking_issues, "symbols": symbols}
    encoded = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    budget.visit()
    if len(encoded) > LIMIT:
        raise ReadLimit("The crossing snapshot exceeds 2 MiB.")
    result = {"status": "ok", "snapshot_json": encoded}
    if audit is not None:
        transaction = audit.get("trial_transaction")
        absent = [uid(value) for value in bounded_list(audit.get("absent_opening_uids", []), 10)]
        if len(set(absent)) != len(absent) or (absent and not transaction):
            raise ValueError("Use distinct opening UIDs from the verified trial transaction.")
        if any(document.GetElement(identifier) is not None for identifier in absent):
            raise ValueError("A trial opening is still present after rollback.")
        result.update(audit_token=uuid4().hex, inventory_sha256=fingerprint(project_inventory(document, DB, budget)),
                      trial_transaction=transaction, absent_opening_uids=absent, kept=kept)
    return result


try:
    active_document, query_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = read(DB, active_document, query_inputs)
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) else type(error).__name__
        result = {"status": "stopped", "message": message}
