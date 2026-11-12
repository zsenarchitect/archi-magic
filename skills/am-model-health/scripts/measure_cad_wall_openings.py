"""Read wall finish planes and door cut spans from temporary Revit elements."""

from contextlib import ExitStack
import math
from time import monotonic

from pyrevit.api import DB

from cad_layout_preview import direction, dot, sub
from check_cad_wall_collisions import existing_solids
from read_cad_room_boundaries import xyz_mm
from read_linked_cad_layout import MM_PER_FOOT

MAX_GEOMETRY = 5000
MAX_SOLID_PROBES = 1000


def wall_faces(wall, axis, width, z, height, tolerance):
    a, b = axis
    dx, dy = direction(a, b)
    perpendicular = (-dy, dx)
    observed = []
    for side in (DB.ShellLayerType.Interior, DB.ShellLayerType.Exterior):
        faces = DB.HostObjectUtils.GetSideFaces(wall, side)
        offsets = []
        for reference in faces:
            face = wall.GetGeometryObjectFromReference(reference)
            if not isinstance(face, DB.PlanarFace):
                raise ValueError("The wall has a nonplanar finish face.")
            native = face.FaceNormal
            normal = (float(native.X), float(native.Y), float(native.Z))
            if (not all(math.isfinite(value) for value in normal) or abs(normal[2]) > 1e-6
                    or abs(dot(normal[:2], perpendicular)) < 1 - 1e-6):
                raise ValueError("The wall finish face is not parallel to the reviewed axis.")
            origin = xyz_mm(face.Origin)
            offset = dot(sub(origin, a), perpendicular)
            sign = 1 if offset > 0 else -1
            for xy in axis:
                for elevation in (z, z + height):
                    expected = (xy[0] + perpendicular[0] * sign * width / 2,
                                xy[1] + perpendicular[1] * sign * width / 2, elevation)
                    residual = abs(sum((p - q) * n for p, q, n in zip(expected, origin, normal)))
                    if residual > tolerance:
                        raise ValueError("A wall finish face differs from the reviewed wall width or axis.")
            offsets.append(offset)
        if not offsets or min(offsets) <= 0 <= max(offsets):
            raise ValueError("The wall finish faces do not identify one side of the axis.")
        observed.append(sorted(round(offset, 6) for offset in offsets))
    if observed[0][0] * observed[1][0] >= 0:
        raise ValueError("The measured wall finish faces are on the same side.")
    return sorted(observed)


def occupied_intervals(bodies, line, origin, axis, options, deadline, remaining):
    intervals = []
    for solid in bodies:
        if remaining[0] == 0 or monotonic() >= deadline:
            raise ValueError("Door cut measurement exceeded its budget.")
        remaining[0] -= 1
        hit = solid.IntersectWithCurve(line, options)
        try:
            if hit.SegmentCount > MAX_GEOMETRY - len(intervals):
                raise ValueError("Door cut intersection returned too many curve segments.")
            for index in range(hit.SegmentCount):
                segment = hit.GetCurveSegment(index)
                try:
                    endpoints = (xyz_mm(segment.GetEndPoint(end))[:2] for end in (0, 1))
                    intervals.append(sorted(dot(sub(point, origin), axis) for point in endpoints))
                finally:
                    segment.Dispose()
        finally:
            hit.Dispose()
    return sorted(intervals)


def cut_span(intervals, low, high, expected, tolerance):
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + tolerance:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if (len(merged) != 2 or abs(merged[0][0] - low) > tolerance or abs(merged[1][1] - high) > tolerance
            or any(abs(value - target) > tolerance for value, target in zip(
                (merged[0][1], merged[1][0]), expected))):
        raise ValueError("The host solid does not have the reviewed door cut span.")
    return [round(merged[0][1], 6), round(merged[1][0], 6)]


def door_cut(wall, bodies, door, neighbors, elevation, tolerance, options, deadline, remaining):
    a, b = wall["axis"]
    axis = direction(a, b)
    start, end = door["span_mm"]
    left = max([0, *(row["span_mm"][1] for row in neighbors if row["span_mm"][1] <= start)])
    right = min([math.dist(a, b), *(row["span_mm"][0] for row in neighbors if row["span_mm"][0] >= end)])
    margin = min(50, (start - left) / 2, (right - end) / 2)
    if margin <= tolerance:
        raise ValueError("The door cut has insufficient wall beside its reviewed span.")
    low, high = start - margin, end + margin
    samples = []
    for lateral in (-0.375 * wall["width_mm"], 0, 0.375 * wall["width_mm"]):
        points = [DB.XYZ((a[0] + along * axis[0] - lateral * axis[1]) / MM_PER_FOOT,
                         (a[1] + along * axis[1] + lateral * axis[0]) / MM_PER_FOOT,
                         elevation / MM_PER_FOOT) for along in (low, high)]
        line = DB.Line.CreateBound(*points)
        try:
            intervals = occupied_intervals(bodies, line, a, axis, options, deadline, remaining)
            samples.append({"lateral_mm": lateral, "span_mm": cut_span(intervals, low, high, (start, end), tolerance)})
        finally:
            line.Dispose()
    return samples


def measure_walls_and_doors(preview, walls, deadline):
    proposal = preview["proposal"]
    z, tolerance = proposal["level_mm"], proposal["tolerance_mm"]
    faces, cuts = {}, {}
    geometry_budget, probe_budget = [MAX_GEOMETRY], [MAX_SOLID_PROBES]
    with ExitStack() as options_stack:
        options = DB.Options()
        options_stack.callback(options.Dispose)
        options.DetailLevel = DB.ViewDetailLevel.Fine
        options.IncludeNonVisibleObjects = False
        intersection_options = DB.SolidCurveIntersectionOptions()
        options_stack.callback(intersection_options.Dispose)
        intersection_options.ResultType = DB.SolidCurveIntersectionMode.CurveSegmentsInside
        for row in proposal["walls"]:
            if monotonic() >= deadline:
                raise ValueError("Wall geometry measurement exceeded its time budget.")
            identifier = row["id"]
            wall = walls[identifier]
            height = preview["wall_choices"][identifier]["height_mm"]
            faces[identifier] = wall_faces(wall, row["axis"], row["width_mm"], z, height, tolerance)
            doors = [door for door in proposal["doors"] if door["host"] == identifier]
            if not doors:
                continue
            geometry = wall.get_Geometry(options)
            with ExitStack() as transformed:
                bodies = []
                for solid, transform in existing_solids(geometry, deadline, geometry_budget):
                    if transform is not None and not transform.IsIdentity:
                        solid = DB.SolidUtils.CreateTransformed(solid, transform)
                        transformed.callback(solid.Dispose)
                    bodies.append(solid)
                if not bodies:
                    raise ValueError("The door host has no measurable solid geometry.")
                for door in doors:
                    marker = door["source_ids"][0]
                    elevation = z + preview["door_choices"][marker]["cut_probe_height_mm"]
                    cuts[marker] = {"elevation_mm": elevation, "samples": door_cut(
                        row, bodies, door, doors, elevation, tolerance, intersection_options, deadline, probe_budget)}
    return {"wall_face_offsets_mm": faces, "door_cuts": cuts}
