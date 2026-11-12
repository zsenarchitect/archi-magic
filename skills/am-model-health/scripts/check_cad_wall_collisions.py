"""Measure overlap between proposed wall bodies and existing host solids."""

from contextlib import ExitStack
import math
from time import monotonic

from System.Collections.Generic import List
from pyrevit.api import DB

from cad_layout_preview import direction
from read_linked_cad_layout import MM_PER_FOOT, element_id

MAX_GEOMETRY = 5000
MAX_INTERSECTIONS = 1000
MAX_DEPTH = 12
NON_SOLID_CATEGORIES = {"OST_Rooms", "OST_RoomSeparationLines", "OST_RvtLinks"}


def wall_solid(wall, height_mm, level_mm):
    a, b = wall["axis"]
    ux, uy = direction(a, b)
    half = wall["width_mm"] / 2
    nx, ny = -uy * half, ux * half
    corners = [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny),
               (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]
    points = [DB.XYZ(x / MM_PER_FOOT, y / MM_PER_FOOT, level_mm / MM_PER_FOOT) for x, y in corners]
    with ExitStack() as temporary:
        loop = DB.CurveLoop()
        temporary.callback(loop.Dispose)
        for start, end in zip(points, points[1:] + points[:1]):
            line = DB.Line.CreateBound(start, end)
            temporary.callback(line.Dispose)
            loop.Append(line)
        profiles = List[DB.CurveLoop]()
        profiles.Add(loop)
        return DB.GeometryCreationUtilities.CreateExtrusionGeometry(
            profiles, DB.XYZ.BasisZ, height_mm / MM_PER_FOOT)


def existing_solids(geometry, deadline, remaining, transform=None, depth=0):
    if geometry is None or depth > MAX_DEPTH:
        raise ValueError("geometry_unavailable")
    for item in geometry:
        if remaining[0] == 0 or monotonic() >= deadline:
            raise ValueError("geometry_budget")
        remaining[0] -= 1
        if isinstance(item, DB.GeometryInstance):
            nested = item.Transform if transform is None else transform.Multiply(item.Transform)
            yield from existing_solids(item.GetSymbolGeometry(), deadline, remaining, nested, depth + 1)
        elif isinstance(item, DB.Solid):
            if item.Faces.Size:
                if not math.isfinite(float(item.Volume)) or item.Volume <= 0:
                    raise ValueError("invalid_existing_solid")
                yield item, transform
        elif not isinstance(item, (DB.Curve, DB.Point, DB.PolyLine)):
            raise ValueError("unsupported_geometry")


def overlap_mm3(proposed, existing):
    intersection = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
        proposed, existing, DB.BooleanOperationsType.Intersect)
    try:
        volume = float(intersection.Volume) * MM_PER_FOOT ** 3
        if not math.isfinite(volume) or volume < 0:
            raise ValueError("invalid_intersection")
        return volume
    finally:
        intersection.Dispose()


def check_wall_collisions(document, proposal, wall_choices, scan, deadline):
    walls = {wall["id"]: wall for wall in proposal["walls"]}
    measured, unresolved, other_checks = [], [], []
    remaining = [MAX_GEOMETRY]
    intersections = 0
    limit = None
    with ExitStack() as temporary:
        solids = {}
        for row in scan["candidates"]:
            wall_ids = sorted(set(row["near"]) & walls.keys())
            non_walls = sorted(set(row["near"]) - walls.keys())
            if non_walls:
                other_checks.append({"id": row["id"], "near": non_walls})
            if not wall_ids:
                continue
            if monotonic() >= deadline:
                limit = "time"
                break
            if row["category"] in NON_SOLID_CATEGORIES:
                unresolved.append({"id": row["id"], "walls": wall_ids, "reason": "requires_native_boundary_or_link_check"})
                continue
            try:
                element = document.GetElement(element_id(row["id"]))
                if element is None or str(element.UniqueId) != row["uid"] or str(element.VersionGuid) != row["version_guid"]:
                    raise ValueError("candidate_changed")
                with ExitStack() as geometry_objects:
                    options = DB.Options()
                    geometry_objects.callback(options.Dispose)
                    options.DetailLevel = DB.ViewDetailLevel.Fine
                    options.IncludeNonVisibleObjects = False
                    geometry = element.get_Geometry(options)
                    bodies = []
                    for solid, transform in existing_solids(geometry, deadline, remaining):
                        if transform is not None and not transform.IsIdentity:
                            solid = DB.SolidUtils.CreateTransformed(solid, transform)
                            geometry_objects.callback(solid.Dispose)
                        bodies.append(solid)
                    if not bodies:
                        raise ValueError("no_existing_solids")
                    if intersections + len(bodies) * len(wall_ids) > MAX_INTERSECTIONS:
                        limit = "intersections"
                        break
                    observations = []
                    for wall_id in wall_ids:
                        if wall_id not in solids:
                            solids[wall_id] = wall_solid(walls[wall_id], wall_choices[wall_id]["height_mm"],
                                                        proposal["level_mm"])
                            temporary.callback(solids[wall_id].Dispose)
                        largest = 0
                        for body in bodies:
                            if monotonic() >= deadline:
                                raise ValueError("geometry_budget")
                            intersections += 1
                            largest = max(largest, overlap_mm3(solids[wall_id], body))
                        observations.append({"id": row["id"], "wall_id": wall_id, "maximum_overlap_mm3": largest})
                    measured.extend(observations)
            except Exception as error:
                unresolved.append({"id": row["id"], "walls": wall_ids,
                                   "reason": str(error) if type(error) is ValueError else type(error).__name__})
    conflicts = sum(row["maximum_overlap_mm3"] > 0 for row in measured)
    incomplete = bool(limit or unresolved or scan["status"] == "incomplete")
    return {"status": "incomplete" if incomplete else "overlap_found" if conflicts else "none_measured",
            "limit": limit, "overlap_count": conflicts, "measured": measured,
            "unresolved": unresolved, "other_checks": other_checks}
