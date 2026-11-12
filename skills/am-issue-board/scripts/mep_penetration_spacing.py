"""Measure spacing between measured opening outlines in one planar host frame."""

import argparse
import json
import math
import sys


def scalar(raw):
    if type(raw) not in (int, float) or not math.isfinite(raw):
        raise ValueError("Use finite host-frame dimensions and coordinates.")
    return float(raw)


def profile(record):
    center = tuple(scalar(value) for value in record["center"])
    if len(center) != 2:
        raise ValueError("Use two coordinates in the reviewed host face frame.")
    if record["shape"] == "round":
        radius = scalar(record["diameter"]) / 2
        if radius <= 0:
            raise ValueError("The opening diameter must be positive.")
        return center, radius
    if record["shape"] != "rectangular":
        raise ValueError("The opening profile is unsupported.")
    width = scalar(record["width"]) / 2
    height = scalar(record["height"]) / 2
    if width <= 0 or height <= 0:
        raise ValueError("Opening width and height must be positive.")
    angle = scalar(record["angle_rad"])
    return center, (width, height, math.cos(angle), math.sin(angle))


def vertices(center, dimensions):
    x, y = center
    width, height, cos, sin = dimensions
    return [(x + a * cos - b * sin, y + a * sin + b * cos)
            for a, b in ((-width, -height), (width, -height), (width, height), (-width, height))]


def edges(polygon):
    return zip(polygon, polygon[1:] + polygon[:1])


def point_to_segment(point, start, end):
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length == 0 or not math.isfinite(length):
        raise ValueError("The opening outline has an invalid edge.")
    ux, uy = dx / length, dy / length
    x, y = point[0] - start[0], point[1] - start[1]
    along = max(0.0, min(length, x * ux + y * uy))
    return math.hypot(x - along * ux, y - along * uy)


def rectangles_distance(first, second):
    for polygon in (first, second):
        for start, end in edges(polygon):
            nx, ny = end[1] - start[1], start[0] - end[0]
            a = [nx * x + ny * y for x, y in first]
            b = [nx * x + ny * y for x, y in second]
            if not all(math.isfinite(value) for value in a + b):
                raise ValueError("The projected opening outline is outside the supported numeric range.")
            if max(a) < min(b) or max(b) < min(a):
                return min(
                    point_to_segment(point, start, end)
                    for points, opposite in ((first, second), (second, first))
                    for point in points
                    for start, end in edges(opposite)
                )
    return 0.0


def circle_rectangle_distance(center, radius, rect_center, dimensions):
    width, height, cos, sin = dimensions
    dx, dy = center[0] - rect_center[0], center[1] - rect_center[1]
    x, y = dx * cos + dy * sin, -dx * sin + dy * cos
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("The projected opening center is outside the supported numeric range.")
    gap = math.hypot(max(0.0, abs(x) - width), max(0.0, abs(y) - height))
    return max(0.0, gap - radius)


def profile_distance(first, second):
    center_a, size_a = first
    center_b, size_b = second
    if isinstance(size_a, float) and isinstance(size_b, float):
        return max(0.0, math.dist(center_a, center_b) - size_a - size_b)
    if isinstance(size_a, float):
        return circle_rectangle_distance(center_a, size_a, center_b, size_b)
    if isinstance(size_b, float):
        return circle_rectangle_distance(center_b, size_b, center_a, size_a)
    relative_center = (center_b[0] - center_a[0], center_b[1] - center_a[1])
    polygons = vertices((0.0, 0.0), size_a), vertices(relative_center, size_b)
    if any(not math.isfinite(coordinate) for polygon in polygons for point in polygon for coordinate in point):
        raise ValueError("The opening outline is outside the supported numeric range.")
    return rectangles_distance(*polygons)


def spacing_conflicts(proposed, existing, minimum_web):
    """Return each insufficient gap involving a proposal. All outlines use host feet."""
    minimum_web = scalar(minimum_web)
    if minimum_web <= 0:
        raise ValueError("Review a positive minimum material web before comparing openings.")
    known = {}
    for row in [*proposed, *existing]:
        key = row["key"]
        if not isinstance(key, str) or not key.strip() or key in known:
            raise ValueError("Supply one unique key for each measured opening outline.")
        known[key] = profile(row)
    conflicts = []
    for index, row in enumerate(proposed):
        first = row["key"]
        for other in [*proposed[index + 1:], *existing]:
            second = other["key"]
            distance = profile_distance(known[first], known[second])
            if not math.isfinite(distance):
                raise ValueError("The spacing measurement is outside the supported numeric range.")
            if distance < minimum_web:
                conflicts.append({"opening": first, "neighbor": second, "gap_ft": distance})
    return conflicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", help="Prepared spacing evidence for one host face frame.")
    args = parser.parse_args()
    try:
        with open(args.evidence, "rb") as stream:
            cap = 2 * 1024 * 1024
            payload = stream.read(cap + 1)
        if len(payload) > cap:
            raise ValueError("The spacing evidence exceeds 2 MiB.")
        record = json.loads(payload)
        host = record["host_uid"]
        if (record["kind"] != "mep-penetration-host-spacing" or record["frame"] != "host_face_internal_feet"
                or not isinstance(host, str) or not host.strip() or record["complete"] is not True):
            raise ValueError("Use complete measured outlines for one reviewed host face frame.")
        proposed, existing = record["proposed"], record["existing"]
        if not isinstance(proposed, list) or len(proposed) > 10 or not isinstance(existing, list):
            raise ValueError("Review up to ten proposals and the complete existing-cut inventory.")
        conflicts = spacing_conflicts(proposed, existing, record["minimum_web_ft"])
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Spacing check blocked. {error}", file=sys.stderr)
        return 2
    print(json.dumps({"host_uid": host, "conflicts": conflicts}, ensure_ascii=True, allow_nan=False))
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
