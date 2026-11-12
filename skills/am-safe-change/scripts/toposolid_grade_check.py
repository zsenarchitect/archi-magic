# -*- coding: utf-8 -*-
"""Compare two Toposolid or floor top-surface snapshots in internal feet."""

from __future__ import print_function

import argparse
from fractions import Fraction
import io
import json
import math
import sys
import time


MAX_TRIANGLES = 3000
MAX_PAIR_CHECKS = 250000
MAX_INPUT_BYTES = 512 * 1024
MAX_SECONDS = 5
SEAM_TOLERANCE_FT = 1e-7


def cross(origin, end, point):
    return ((end[0] - origin[0]) * (point[1] - origin[1])
            - (end[1] - origin[1]) * (point[0] - origin[0]))


def polygon_area(polygon):
    if len(polygon) < 3:
        return 0
    return abs(sum(cross(polygon[0], polygon[i], polygon[i + 1])
                   for i in range(1, len(polygon) - 1))) / 2


def intersection(first, second):
    polygon = [point[:2] for point in first]
    for index in range(3):
        start, end = second[index], second[(index + 1) % 3]
        if not polygon:
            break
        clipped = []
        previous = polygon[-1]
        previous_side = cross(start, end, previous)
        for point in polygon:
            side = cross(start, end, point)
            if (side < 0) != (previous_side < 0):
                fraction = previous_side / (previous_side - side)
                clipped.append(tuple(a + fraction * (b - a) for a, b in zip(previous, point)))
            if side >= 0:
                clipped.append(point)
            previous, previous_side = point, side
        polygon = clipped
    return polygon


def elevation(triangle, point):
    a, b, c = triangle
    determinant = cross(a, b, c)
    return (a[2] + (b[2] - a[2]) * cross(a, point, c) / determinant
            + (c[2] - a[2]) * cross(a, b, point) / determinant)


def finite(value):
    return type(value) in (int, float) and abs(value) <= 1e9


def triangles(snapshot, side, deadline, now):
    items = []
    for face in snapshot["top_faces"]:
        for raw in face["triangles_ft"]:
            if len(items) >= MAX_TRIANGLES or now() >= deadline:
                raise ValueError("{} mesh exceeds the triangle count or time budget.".format(side))
            if (len(raw) != 3 or any(len(point) != 3 or not all(finite(value) for value in point)
                                     for point in raw)):
                raise ValueError("{} mesh contains an invalid XYZ triangle.".format(side))
            a, b, c = (tuple(Fraction(value) for value in point) for point in raw)
            signed_area = cross(a, b, c)
            if not signed_area:
                raise ValueError("{} mesh contains a degenerate XY triangle.".format(side))
            points = (a, b, c) if signed_area > 0 else (a, c, b)
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            items.append({"side": side, "points": points, "area": abs(signed_area) / 2,
                          "box": (min(xs), max(xs), min(ys), max(ys)), "covered": []})
    if not items:
        raise ValueError("{} mesh has no top triangles.".format(side))
    return items


def compare(source, floor, area_tolerance_sqft, elevation_tolerance_ft):
    if any(not finite(value) or value < 0 for value in (area_tolerance_sqft, elevation_tolerance_ft)):
        raise ValueError("Supply finite nonnegative area and elevation tolerances.")
    now = getattr(time, "monotonic", time.time)
    deadline = now() + MAX_SECONDS
    source_items = triangles(source, "source", deadline, now)
    floor_items = triangles(floor, "floor", deadline, now)
    items = sorted(source_items + floor_items, key=lambda item: item["box"])
    active, checks, max_grade_error = [], 0, 0.0
    for item in items:
        if now() >= deadline:
            raise ValueError("Surface comparison exceeded {} seconds.".format(MAX_SECONDS))
        x_min, _, y_min, y_max = item["box"]
        candidates = []
        for other in active:
            checks += 1
            if checks > MAX_PAIR_CHECKS or now() >= deadline:
                raise ValueError("Surface comparison exceeded its time or {}-pair budget.".format(MAX_PAIR_CHECKS))
            _, other_x_max, other_y_min, other_y_max = other["box"]
            if other_x_max < x_min:
                continue
            candidates.append(other)
            if other_y_max < y_min or other_y_min > y_max:
                continue
            polygon = intersection(item["points"], other["points"])
            if not polygon:
                continue
            area = polygon_area(polygon)
            same_mesh = item["side"] == other["side"]
            if same_mesh and area:
                raise ValueError("{} mesh contains overlapping XY triangles.".format(item["side"]))
            if not same_mesh and not area:
                continue
            error = max(abs(elevation(item["points"], point) - elevation(other["points"], point))
                        for point in polygon)
            if same_mesh:
                if error > SEAM_TOLERANCE_FT:
                    raise ValueError("{} mesh has different elevations at a shared XY location.".format(item["side"]))
            else:
                item["covered"].append(area)
                other["covered"].append(area)
                max_grade_error = max(max_grade_error, float(error))
        candidates.append(item)
        active = candidates

    def coverage(mesh):
        covered, missing = [], []
        for item in mesh:
            if now() >= deadline:
                raise ValueError("Surface comparison exceeded {} seconds.".format(MAX_SECONDS))
            overlap = sum(item["covered"])
            covered.append(float(overlap))
            missing.append(float(item["area"] - overlap))
        return math.fsum(covered), math.fsum(missing)

    overlap, source_missing = coverage(source_items)
    _, floor_extra = coverage(floor_items)
    return {
        "matches_meshes": (overlap > 0 and source_missing <= area_tolerance_sqft
                           and floor_extra <= area_tolerance_sqft and max_grade_error <= elevation_tolerance_ft),
        "source_uncovered_sqft": source_missing, "floor_extra_sqft": floor_extra,
        "overlap_sqft": overlap, "maximum_elevation_error_ft": max_grade_error if overlap else None,
    }


def load(path):
    with io.open(path, "r", encoding="utf-8") as stream:
        payload = stream.read(MAX_INPUT_BYTES + 1)
    if len(payload.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError("A surface input exceeds {} bytes.".format(MAX_INPUT_BYTES))
    result = json.loads(payload)
    result = result.get("result", result)
    if "snapshot_json" in result:
        if result.get("status") != "ok":
            raise ValueError("Supply a successful surface read result.")
        return json.loads(result["snapshot_json"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("floor")
    parser.add_argument("--area-tolerance-sqft", required=True, type=float)
    parser.add_argument("--elevation-tolerance-ft", required=True, type=float)
    args = parser.parse_args()
    try:
        result = compare(load(args.source), load(args.floor), args.area_tolerance_sqft, args.elevation_tolerance_ft)
    except (IOError, KeyError, TypeError, ValueError, AttributeError) as error:
        print(json.dumps({"status": "stopped", "message": str(error)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["matches_meshes"] else 1


if __name__ == "__main__":
    sys.exit(main())
