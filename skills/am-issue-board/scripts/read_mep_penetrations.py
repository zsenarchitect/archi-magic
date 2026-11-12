"""Read bounded linked MEP crossing evidence from Revit without changing the model."""

from itertools import product
import json
import math
from time import monotonic

from issue_board import saved_document_identity
from mep_penetration_preview import fingerprint
from mep_penetration_spacing import point_to_segment, scalar
from structural_compare_cli import LIMIT, bounded_list, checked_id, uid


class ReadLimit(ValueError):
    pass


class ReadBudget:
    def __init__(self):
        self.deadline = monotonic() + 10
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


def face_edges(DB, face, budget):
    loops = list(face.EdgeLoops)
    if len(loops) != 1:
        raise ValueError("The host face contains an existing cut or ambiguous loops.")
    edges = []
    for edge in loops[0]:
        budget.visit()
        curve = edge.AsCurve()
        if not isinstance(curve, DB.Line) or not curve.IsBound:
            raise ValueError("The host face boundary is not a straight-edge outline.")
        edges.append([curve.GetEndPoint(0), curve.GetEndPoint(1)])
    if len(edges) != 4:
        raise ValueError("The host face is outside the supported rectangular scope.")
    return edges


def element_scope(element):
    option = element.DesignOption
    if option is not None or element.DemolishedPhaseId.Value != -1:
        raise ValueError("The element belongs to a design option or has a demolition phase.")
    return {"version_guid": str(element.VersionGuid), "created_phase_id": str(element.CreatedPhaseId.Value)}


def principal_host(DB, element, minimum_web, tolerance, budget):
    scope = element_scope(element)
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
    faces = [element.GetGeometryObjectFromReference(list(rows)[0]) for rows in refs]
    if not all(isinstance(face, DB.PlanarFace) for face in faces):
        raise ValueError("The host principal faces are not planar.")
    first, second = faces
    normal = first.FaceNormal.Negate()
    depth = second.Origin.Subtract(first.Origin).DotProduct(normal)
    if (depth <= tolerance or normal.DotProduct(second.FaceNormal) < 1 - 1e-9
            or (kind == "wall" and abs(normal.Z) > 1e-9) or (kind == "floor" and abs(normal.Z) < 1 - 1e-9)):
        raise ValueError("The host faces are sloped, nonparallel, or have no verified thickness.")

    outlines = [face_edges(DB, face, budget) for face in faces]
    sizes = [math.dist(face_uv(first, a), face_uv(first, b)) for a, b in outlines[0]]
    if (min(sizes) <= tolerance or abs(first.Area - sizes[0] * sizes[1]) > tolerance * max(sizes)
            or abs(second.Area - first.Area) > tolerance * max(sizes)):
        raise ValueError("The host principal outlines are not congruent rectangles.")
    for edge in outlines[0]:
        a, b = edge
        neighbors = [end.Subtract(a) for other in outlines[0] if other is not edge
                     for near, end in (other, other[::-1]) if near.DistanceTo(a) <= tolerance]
        direction = b.Subtract(a).Normalize()
        if len(neighbors) != 1 or abs(direction.DotProduct(neighbors[0].Normalize())) > 1e-9:
            raise ValueError("The measured host outline has an ambiguous corner.")
    first_uv = [face_uv(first, point) for edge in outlines[0] for point in edge]
    second_uv = [face_uv(first, point) for edge in outlines[1] for point in edge]
    if any(math.dist(a, b) > tolerance for a, b in zip(sorted(first_uv), sorted(second_uv))):
        raise ValueError("The host principal outlines differ in the same face frame.")

    solids = native_solids(DB, element, budget)
    if (len(solids) != 1 or solids[0].Faces.Size != 6
            or not all(isinstance(face, DB.PlanarFace) for face in solids[0].Faces)
            or abs(solids[0].Volume - first.Area * depth) > tolerance * first.Area):
        raise ValueError("The host has a cut or solid outside the supported uncut prism scope.")

    corners = sorted(xyz(point) for outline in outlines for edge in outline for point in edge)
    row = dict(element_record(element), kind=kind, normal=xyz(normal), depth_ft=scalar(depth),
               gross_sha256=fingerprint(corners), cuts_complete=True, minimum_web_ft=minimum_web, existing=[],
               **scope)
    return row, (faces, outlines, normal, solids[0], bounds(DB, element, DB.Transform.Identity))


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
    faces, outlines, normal, solid, host_box = host
    line, ends, width_axis, run_box = run
    padding = clearance + tolerance
    if any(low > r_high + padding or r_low - padding > high
           for (low, high), (r_low, r_high) in zip(host_box, run_box)):
        return {"status": "no_crossing"}

    options = DB.SolidCurveIntersectionOptions()
    intersection = None
    try:
        budget.intersect()
        intersection = solid.IntersectWithCurve(line, options)
        budget.visit()
        if intersection.SegmentCount == 0:
            raise ValueError("The swept bounds overlap but the centerline does not prove a crossing.")
        if intersection.SegmentCount != 1:
            raise ValueError("The solid produced multiple crossing segments.")
        segment = intersection.GetCurveSegment(0)
        points = sorted((segment.GetEndPoint(0), segment.GetEndPoint(1)),
                        key=lambda point: point.Subtract(faces[0].Origin).DotProduct(normal))
    finally:
        if intersection is not None:
            intersection.Dispose()
        options.Dispose()

    delta = points[1].Subtract(points[0])
    along = delta.DotProduct(normal)
    if along <= tolerance or delta.Subtract(normal.Multiply(along)).GetLength() > tolerance:
        raise ValueError("The run is not perpendicular to the host faces.")
    stations = [point.Subtract(faces[0].Origin).DotProduct(normal) for point in ends]
    depth = faces[1].Origin.Subtract(faces[0].Origin).DotProduct(normal)
    if min(stations) >= -tolerance or max(stations) <= depth + tolerance:
        raise ValueError("The finite run does not extend past both faces.")

    distances = []
    for face, outline, point in zip(faces, outlines, points):
        budget.visit()
        projected = face.Project(point)
        if projected is None or projected.Distance > tolerance or not face.IsInside(projected.UVPoint):
            raise ValueError("The crossing does not meet both principal face interiors.")
        center = face_uv(face, point)
        distances.extend(point_to_segment(center, face_uv(face, a), face_uv(face, b)) for a, b in outline)
    edge_distance = min(distances)
    if edge_distance <= tolerance:
        raise ValueError("The crossing coincides with or touches a host edge.")
    row = {"status": "solid_crossing", "entry_ft": xyz(points[0]), "exit_ft": xyz(points[1]),
           "center_uv_ft": face_uv(faces[0], points[0]), "edge_distance_ft": edge_distance}
    if width_axis is not None:
        row["angle_rad"] = math.atan2(width_axis.DotProduct(faces[0].YVector), width_axis.DotProduct(faces[0].XVector))
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


def read(DB, document, args):
    if document is None or document.IsFamilyDocument or document.IsModifiable or document.IsModified:
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
    if document.IsModified or linked.IsModified or document.IsModifiable:
        raise ValueError("The host or linked document changed during the read.")
    report = {"kind": "mep-penetration-evidence", "frame": "revit_host_internal_feet",
              "host_identity": project, "link_identity": linked_identity, "link_instance_uid": uid(str(link.UniqueId)),
              "link_instance_id": checked_id(str(link.Id.Value)),
              "link_transform": {key: xyz(getattr(transform, key)) for key in ("Origin", "BasisX", "BasisY", "BasisZ")},
              "clearance_ft": clearance, "tolerance_ft": tolerance, "complete": True,
              "hosts": [row for row, _ in hosts], "runs": [row for row, _ in runs], "pairs": pairs, "tracked": []}
    encoded = json.dumps(report, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    budget.visit()
    if len(encoded) > LIMIT:
        raise ReadLimit("The crossing snapshot exceeds 2 MiB.")
    return {"status": "ok", "snapshot_json": encoded}


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
