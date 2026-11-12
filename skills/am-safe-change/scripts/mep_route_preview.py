"""Preview a bounded MEP centerline between two reviewed open connectors."""

import argparse
from hashlib import sha256
from itertools import permutations
import json
import math
from pathlib import Path
import sys

from mep_sizing_preview import unique_object


EPSILON = 1e-7
DIAGONAL_COSINE = math.sqrt(0.5)
COLUMNS = ["Route", "Length (ft)", "Bends", "Waypoints (host ft)"]


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1_000_000:
        raise ValueError("Use finite coordinates and lengths within one million feet.")
    return float(value)


def point(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("A route point or axis needs three coordinates.")
    return tuple(map(number, value))


def unit(vector):
    length = math.hypot(*vector)
    return tuple(value / length for value in vector) if length > EPSILON else None


def delta(a, b):
    return tuple(end - start for start, end in zip(a, b))


def along(origin, direction, length):
    return tuple(value + axis * length for value, axis in zip(origin, direction))


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def compact(points):
    result = []
    for current in points:
        if result and math.dist(result[-1], current) <= EPSILON:
            continue
        if len(result) >= 2:
            incoming, outgoing = unit(delta(*result[-2:])), unit(delta(result[-1], current))
            if dot(incoming, outgoing) >= 1 - EPSILON:
                result.pop()
        result.append(current)
    return result


def axis_path(start, end, axes):
    current = list(start)
    for axis in axes:
        current[axis] = end[axis]
        yield tuple(current)


def intersects(a, b, box):
    enter, leave = 0.0, 1.0
    for start, end, low, high in zip(a, b, *box):
        movement = end - start
        if abs(movement) <= EPSILON:
            if start < low - EPSILON or start > high + EPSILON:
                return False
            continue
        crossings = sorted(((low - EPSILON - start) / movement, (high + EPSILON - start) / movement))
        enter, leave = max(enter, crossings[0]), min(leave, crossings[1])
        if enter > leave:
            return False
    return True


def candidates(start, end, first_axis, last_axis, setback, boxes):
    first = along(start, first_axis, setback)
    last = along(end, last_axis, setback)
    yield [start, end]
    for order in permutations(range(3)):
        yield [start, first, *axis_path(first, last, order), end]
    for low, high in boxes:
        for axis in range(3):
            for plane in (low[axis] - 10 * EPSILON, high[axis] + 10 * EPSILON):
                leading, trailing = list(first), list(last)
                leading[axis] = trailing[axis] = plane
                for order in permutations(index for index in range(3) if index != axis):
                    yield [start, first, tuple(leading), *axis_path(leading, trailing, order), last, end]


def beveled(points, setback):
    result = [points[0]]
    for a, b, c in zip(points, points[1:], points[2:]):
        incoming, outgoing = unit(delta(a, b)), unit(delta(b, c))
        if (abs(dot(incoming, outgoing)) <= EPSILON
                and min(math.dist(a, b), math.dist(b, c)) > 2 * setback):
            result.extend((along(b, incoming, -setback), along(b, outgoing, setback)))
        else:
            result.append(b)
    return result + points[-1:]


def assess(points, first_axis, last_axis, minimum, boxes):
    if len(points) < 2:
        return None
    segments = list(zip(points, points[1:]))
    directions = [unit(delta(a, b)) for a, b in segments]
    lengths = [math.dist(a, b) for a, b in segments]
    if min(lengths) < minimum or not all(directions):
        return None
    if dot(directions[0], first_axis) < 1 - EPSILON or dot(directions[-1], last_axis) > -1 + EPSILON:
        return None
    bends = []
    for a, b in zip(directions, directions[1:]):
        cosine = dot(a, b)
        if abs(cosine) <= EPSILON:
            bends.append(90)
        elif abs(cosine - DIAGONAL_COSINE) <= EPSILON:
            bends.append(45)
        else:
            return None
    if any(intersects(a, b, box) for a, b in segments for box in boxes):
        return None
    return sum(lengths), bends


def preview(data):
    if set(data) != {"endpoints", "minimum_segment_ft", "clearance_ft", "obstacles"}:
        raise ValueError("Supply the reviewed endpoints, minimum segment, clearance and obstacle boxes.")
    evidence = data["endpoints"]
    if (evidence["kind"] != "mep-route-endpoints" or type(evidence["version"]) is not int
            or evidence["version"] != 1 or len(evidence["endpoints"]) != 2
            or not isinstance(evidence["source"], dict)):
        raise ValueError("Supply a reviewed pair from the Revit connector endpoint query.")
    first, last = evidence["endpoints"]
    if first["domain"] not in ("DomainHvac", "DomainPiping", "DomainCableTrayConduit") or (
            first["domain"] != last["domain"] or first["shape"] != last["shape"]):
        raise ValueError("The endpoints need the same supported connector domain and profile.")
    if (first["element_id"], first["connector_id"]) == (last["element_id"], last["connector_id"]):
        raise ValueError("Supply two distinct reviewed connector endpoints.")
    size_count = {"Round": 1, "Rectangular": 2}.get(first["shape"])
    size = first["dimensions_ft"]
    if (size_count is None or not isinstance(size, list) or len(size) != size_count
            or size != last["dimensions_ft"] or any(number(value) <= 0 for value in size)):
        raise ValueError("Supply matching round or rectangular connector dimensions.")
    start, end = point(first["origin_ft"]), point(last["origin_ft"])
    first_axis, last_axis = (point(endpoint["axes"][2]) for endpoint in (first, last))
    if any(abs(math.hypot(*axis) - 1) > EPSILON for axis in (first_axis, last_axis)):
        raise ValueError("Connector normals must be unit vectors.")
    minimum = number(data["minimum_segment_ft"])
    clearance = number(data["clearance_ft"])
    if minimum <= EPSILON or clearance < 0:
        raise ValueError("Supply a positive minimum segment length and a nonnegative clearance.")
    obstacles = data["obstacles"]
    if not isinstance(obstacles, list) or len(obstacles) > 32:
        raise ValueError("Supply up to 32 reviewed obstacle boxes.")
    padding = math.hypot(*size) / 2 + clearance
    boxes = []
    for obstacle in obstacles:
        if set(obstacle) != {"min_ft", "max_ft"}:
            raise ValueError("Each obstacle needs only its minimum and maximum host-space coordinates.")
        low, high = point(obstacle["min_ft"]), point(obstacle["max_ft"])
        if any(a >= b for a, b in zip(low, high)):
            raise ValueError("Each obstacle needs increasing minimum and maximum coordinates.")
        boxes.append((tuple(value - padding for value in low), tuple(value + padding for value in high)))

    best = None
    for raw in candidates(start, end, first_axis, last_axis, 2 * minimum, boxes):
        path = compact(raw)
        for option in (path, compact(beveled(path, minimum)) if len(path) >= 3 else path):
            result = assess(option, first_axis, last_axis, minimum, boxes)
            if result is not None:
                length, bends = result
                candidate = (length, len(bends), option, bends)
                if best is None or candidate < best:
                    best = candidate

    before = [["candidate", None, None, None]]
    warnings = []
    route = None
    if best is None:
        after = before
        warnings.append({"row": "candidate", "blocking": True,
                         "text": "The search found no route with compatible turns and supplied clearances."})
    else:
        length, _, path, bends = best
        route = {"source": evidence["source"], "endpoints": evidence["endpoints"],
                 "points_ft": path, "length_ft": length, "bends_degrees": bends}
        after = [["candidate", length, ", ".join(map(str, bends)), str(path)]]
    return {"kind": "change-preview", "recipe": "mep-route", "title": "MEP route",
            "keyColumn": "Route", "columns": COLUMNS, "changedColumns": COLUMNS[1:],
            "before": before, "after": after, "warnings": warnings, "route": route,
            "review_only": True, "blocked": bool(warnings), "steps": [], "ownership_ids": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Reviewed connector pair, obstacle boxes and clearances")
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("The route input exceeds 256 KiB.")
        result = preview(json.loads(raw, object_pairs_hook=unique_object))
        result["basis_sha256"] = sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError) as error:
        print(f"MEP route stopped. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    return 1 if result["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
