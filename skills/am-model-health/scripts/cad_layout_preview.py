"""Propose walls, door locations and rooms from a local CAD geometry snapshot."""

import argparse
from collections import Counter
import hashlib
from itertools import combinations
import json
import math
import os
import stat
import sys

from cad_file_inventory import link_or_reparse, local_path

INPUT_LIMIT = 1024 * 1024
FRAME = "revit_internal_axes_mm"


def read_input(path, limit=INPUT_LIMIT):
    source = local_path(path)
    original = source.lstat()
    if link_or_reparse(original) or not stat.S_ISREG(original.st_mode):
        raise ValueError("Use a regular local JSON file.")
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(os.open(source, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError(f"Use a regular JSON file up to {limit // (1024 * 1024)} MiB.")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = source.lstat()
        if not (
            len(raw) == before.st_size == after.st_size == current.st_size
            and before.st_mtime_ns == after.st_mtime_ns == current.st_mtime_ns
            and os.path.samestat(before, current)
        ):
            raise ValueError("Input changed while being read.")

    def keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Repeated JSON field.")
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=keys), hashlib.sha256(raw).hexdigest()


def fields(item, required):
    if not isinstance(item, dict) or set(item) != set(required.split()):
        raise ValueError(f"Expected fields: {required}.")
    return item


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 160 or not value.isprintable():
        raise ValueError("Use a short printable identifier.")
    return value


def number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError("A number is outside the permitted range.")
    return float(value)


def point(raw, level, tolerance):
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValueError("A point must contain X, Y and Z in millimetres.")
    x, y, z = (number(value, -1e7, 1e7) for value in raw)
    if abs(z - level) > tolerance:
        raise ValueError("A primitive is off the selected level.")
    return x, y


def sub(a, b):
    return a[0] - b[0], a[1] - b[1]


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def scale(a, ratio):
    return a[0] * ratio, a[1] * ratio


def direction(a, b):
    delta = sub(b, a)
    return scale(delta, 1 / math.hypot(*delta))


def intersection(a, b):
    p, q = a
    r, s = b
    u, v = sub(q, p), sub(s, r)
    determinant = cross(u, v)
    if abs(determinant) <= 1e-9:
        return None
    delta = sub(r, p)
    return cross(delta, v) / determinant, cross(delta, u) / determinant


def point_distance(p, segment):
    a, b = segment
    delta = sub(b, a)
    projection = max(0, min(1, dot(sub(p, a), delta) / dot(delta, delta)))
    return math.dist(sub(p, a), scale(delta, projection))


def edge_distance(a, b):
    hit = intersection(a, b)
    if hit is not None and all(0 <= value <= 1 for value in hit):
        return 0
    return min(*(point_distance(p, b) for p in a), *(point_distance(p, a) for p in b))


def axis_frame(a, b):
    u, v = (direction(*line) for line in (a, b))
    if dot(u, v) < 0:
        v = scale(v, -1)
    if dot(u, v) < math.cos(math.radians(0.5)):
        return None
    axis = direction((0, 0), (u[0] + v[0], u[1] + v[1]))
    if axis < (0, 0):
        axis = scale(axis, -1)
    normal = -axis[1], axis[0]
    projected = [sorted((dot(p, axis), dot(p, normal)) for p in line) for line in (a, b)]
    start = max(projected[0][0][0], projected[1][0][0])
    end = min(projected[0][1][0], projected[1][1][0])
    return axis, normal, projected, start, end


def offset(projected, position):
    (x1, y1), (x2, y2) = projected
    return y1 + (y2 - y1) * ((position - x1) / (x2 - x1))


def wall_candidate(first, second, width, tolerance, minimum):
    frame = axis_frame(first, second)
    if not frame:
        return None
    axis, normal, projections, start, end = frame
    if end - start < minimum:
        return None
    tails = [(start - line[0][0], line[1][0] - end) for line in projections]
    offsets = [[offset(line, x) for line in projections] for x in (start, end)]
    signed = [b - a for a, b in offsets]
    if signed[0] * signed[1] <= 0:
        return None
    residual = max(abs(abs(value) - width) / 2 for value in signed)
    if residual > tolerance:
        return None
    axis_points = [
        (axis[0] * x + normal[0] * (a + b) / 2, axis[1] * x + normal[1] * (a + b) / 2)
        for x, (a, b) in zip((start, end), offsets)
    ]
    return {"axis": axis_points, "overlap_mm": end - start, "face_residual_mm": residual,
            "tail_lengths_mm": tails, "width_mm": width}


def walls_conflict(a, b, tolerance):
    left, right = a["axis"], b["axis"]
    frame = axis_frame(left, right)
    if frame:
        _, _, lines, start, end = frame
        if end - start > tolerance:
            gaps = [offset(lines[0], x) - offset(lines[1], x) for x in (start, end)]
            distance = 0 if gaps[0] * gaps[1] <= 0 else min(map(abs, gaps))
            if distance < (a["width_mm"] + b["width_mm"]) / 2 - 1e-8:
                return True
    hit = intersection(left, right)
    return hit is not None and all(
        tolerance < t * math.dist(*line) < math.dist(*line) - tolerance
        for t, line in zip(hit, (left, right))
    )


def door_span(line, wall, width, tolerance, setback):
    a, b = wall["axis"]
    axis = direction(a, b)
    delta = [sub(p, a) for p in line]
    if max(abs(cross(axis, p)) for p in delta) > tolerance:
        return None
    low, high = sorted(dot(axis, p) for p in delta)
    if (low < setback or high > math.dist(a, b) - setback
            or abs(high - low - width) > tolerance):
        return None
    return low, high


def edges(loop):
    return list(zip(loop, loop[1:] + loop[:1]))


def inside(p, sides):
    x, y = p
    return sum(
        (y1 > y) != (y2 > y) and x < x1 + (x2 - x1) * (y - y1) / (y2 - y1)
        for (x1, y1), (x2, y2) in sides
    ) % 2 == 1


def valid_room(loop, seed, tolerance):
    sides = edges(loop)
    if len(set(loop)) != len(loop) or min(math.dist(*edge) for edge in sides) <= tolerance:
        return False
    base = loop[0]
    area = abs(sum(cross(sub(a, base), sub(b, base)) for a, b in sides)) / 2
    if area < 10000:
        return False
    for i, (a, b) in enumerate(sides):
        c = sides[(i + 1) % len(sides)][1]
        u, v = sub(a, b), sub(c, b)
        if dot(u, v) > 0 and abs(cross(direction(b, a), v)) <= tolerance:
            return False
    for i, j in combinations(range(len(sides)), 2):
        if j != i + 1 and (i, j) != (0, len(sides) - 1):
            if edge_distance(sides[i], sides[j]) <= tolerance:
                return False
    return inside(seed, sides) and all(point_distance(seed, edge) > tolerance for edge in sides)


def rooms_conflict(a, b, tolerance):
    first, second = edges(a), edges(b)
    return (
        inside(a[0], second) or inside(b[0], first)
        or any(edge_distance(left, right) <= tolerance for left in first for right in second)
    )


def propose(snapshot, mapping):
    fields(snapshot, "coordinate_frame link_id level_id level_mm segments room_loops")
    fields(mapping, "tolerance_mm minimum_overlap_mm door_setback_mm walls doors room_layers")
    if snapshot["coordinate_frame"] != FRAME:
        raise ValueError("Use Revit internal axes expressed in millimetres.")
    link_id, level_id = (label(snapshot[key]) for key in ("link_id", "level_id"))
    level = number(snapshot["level_mm"], -1e7, 1e7)
    tolerance = number(mapping["tolerance_mm"], 0.1, 25)
    minimum = number(mapping["minimum_overlap_mm"], 100, 1e6)
    setback = number(mapping["door_setback_mm"], tolerance, 1e5)
    wall_map, door_map, room_layers = (mapping[key] for key in ("walls", "doors", "room_layers"))
    if not isinstance(wall_map, dict) or not isinstance(door_map, dict) or not isinstance(room_layers, list):
        raise ValueError("Use layer maps for walls and doors and a room layer list.")
    layers = [label(layer) for layer in [*wall_map, *door_map, *room_layers]]
    if len(layers) > 128 or len(layers) != len(set(layers)):
        raise ValueError("Map up to 128 unique layers, each to one role.")
    for entry in [*wall_map.values(), *door_map.values()]:
        fields(entry, "type_id width_mm")
        label(entry["type_id"])
        number(entry["width_mm"], 30, 10000)

    segments, loops = snapshot["segments"], snapshot["room_loops"]
    if not isinstance(segments, list) or not isinstance(loops, list) or len(segments) > 400 or len(loops) > 32:
        raise ValueError("Use at most 400 segments and 32 room loops.")
    vertex_count = 0
    for loop in loops:
        fields(loop, "id layer closed vertices interior_point")
        if not isinstance(loop["vertices"], list) or not 3 <= len(loop["vertices"]) <= 64:
            raise ValueError("Use between 3 and 64 vertices per room loop.")
        vertex_count += len(loop["vertices"])
    if vertex_count > 512:
        raise ValueError("Use at most 512 room vertices in total.")
    ids, unresolved = set(), []
    wall_lines, markers, room_candidates = [], [], []

    def source(item):
        token, layer = label(item["id"]), label(item["layer"])
        if token in ids:
            raise ValueError("Each primitive needs a unique identifier.")
        ids.add(token)
        return token, layer

    def warn(tokens, reason, **details):
        unresolved.append({"source_ids": tokens, "reason": reason, **details})

    for item in segments:
        fields(item, "id layer start end")
        token, layer = source(item)
        try:
            line = [point(item[key], level, tolerance) for key in ("start", "end")]
            if math.dist(*line) <= tolerance:
                raise ValueError("A segment is shorter than the tolerance.")
        except ValueError as exc:
            warn([token], str(exc))
            continue
        if layer in wall_map:
            wall_lines.append((token, layer, line))
        elif layer in door_map:
            markers.append((token, layer, line))
        else:
            warn([token], "No segment mapping for this layer.")

    candidates, clipped = [], {}
    for (a_id, a_layer, a), (b_id, b_layer, b) in combinations(wall_lines, 2):
        if a_layer != b_layer:
            continue
        target = wall_map[a_layer]
        match = wall_candidate(a, b, target["width_mm"], tolerance, minimum)
        if match is None:
            continue
        largest_tail = max(max(lengths) for lengths in match["tail_lengths_mm"])
        if largest_tail <= tolerance:
            candidates.append({**match, "source_ids": [a_id, b_id], "type_id": target["type_id"]})
            if len(candidates) > 4000:
                raise ValueError("Too many competing wall pairs. Narrow the layer scope.")
        else:
            for token, partner in ((a_id, b_id), (b_id, a_id)):
                if token not in clipped or largest_tail < clipped[token]["maximum_tail_mm"]:
                    clipped[token] = {"candidate_partner": partner, "maximum_tail_mm": largest_tail}
    used = Counter(token for wall in candidates for token in wall["source_ids"])
    walls = [wall for wall in candidates if all(used[token] == 1 for token in wall["source_ids"])]
    matched = {token for wall in walls for token in wall["source_ids"]}
    for token, _, _ in wall_lines:
        if token not in matched:
            warn([token], "Wall face is ambiguous or has no partner covering its length within tolerance.",
                 **clipped.get(token, {}))
    conflicts = set()
    for (i, a), (j, b) in combinations(enumerate(walls), 2):
        if walls_conflict(a, b, tolerance):
            conflicts.update((i, j))
    for index in sorted(conflicts):
        warn(walls[index]["source_ids"], "Wall proposal overlaps or crosses another candidate.")
    walls = [wall for i, wall in enumerate(walls) if i not in conflicts]
    for index, wall in enumerate(walls, 1):
        wall["id"] = f"wall-{index}"

    doors = []
    for token, layer, line in markers:
        target = door_map[layer]
        hosts = [(wall, span) for wall in walls
                 if (span := door_span(line, wall, target["width_mm"], tolerance, setback))]
        if len(hosts) != 1:
            warn([token], "Door marker does not fit one unique wall and opening type.")
            continue
        host, span = hosts[0]
        a, b = host["axis"]
        x, y = scale(direction(a, b), sum(span) / 2)
        doors.append({"source_ids": [token], "host": host["id"], "type_id": target["type_id"],
                      "location": [a[0] + x, a[1] + y], "span_mm": span, "marker": line,
                      "facing_requires_confirmation": True})
    conflicts = set()
    for (i, a), (j, b) in combinations(enumerate(doors), 2):
        if a["host"] == b["host"] and max(a["span_mm"][0], b["span_mm"][0]) <= min(
            a["span_mm"][1], b["span_mm"][1]
        ) + tolerance:
            conflicts.update((i, j))
    for index in sorted(conflicts):
        warn(doors[index]["source_ids"], "Door spans overlap or touch on the proposed host.")
    doors = [door for i, door in enumerate(doors) if i not in conflicts]

    for item in loops:
        token, layer = source(item)
        try:
            loop = [point(value, level, tolerance) for value in item["vertices"]]
            seed = point(item["interior_point"], level, tolerance)
            if layer not in room_layers or item["closed"] is not True or not valid_room(loop, seed, tolerance):
                raise ValueError("Room boundary or interior point is invalid or unmapped.")
        except ValueError as exc:
            warn([token], str(exc))
            continue
        room_candidates.append({"source_ids": [token], "boundary": loop, "interior_point": seed})
    conflicts = set()
    for (i, a), (j, b) in combinations(enumerate(room_candidates), 2):
        if rooms_conflict(a["boundary"], b["boundary"], tolerance):
            conflicts.update((i, j))
    for index in sorted(conflicts):
        warn(room_candidates[index]["source_ids"], "Room loops overlap, touch or share a boundary.")
    rooms = [room for i, room in enumerate(room_candidates) if i not in conflicts]

    status = "unresolved" if unresolved else "proposed" if walls or doors or rooms else "empty"
    return {
        "kind": "cad-layout-proposal", "status": status,
        "model_write_approved": False, "coordinate_frame": FRAME,
        "link_id": link_id, "level_id": level_id, "level_mm": level, "tolerance_mm": tolerance,
        "walls": walls, "doors": doors, "rooms": rooms, "unresolved": unresolved,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    parser.add_argument("--mapping", required=True)
    args = parser.parse_args(argv)
    try:
        snapshot, source_sha = read_input(args.snapshot)
        mapping, mapping_sha = read_input(args.mapping)
        result = propose(snapshot, mapping)
        result.update(snapshot_sha256=source_sha, mapping_sha256=mapping_sha)
        encoded = json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
        if len(encoded) > 2 * INPUT_LIMIT:
            raise ValueError("Proposal exceeds 2 MiB.")
        sys.stdout.write(encoded)
        return 1 if result["unresolved"] else 0
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        print(f"CAD layout proposal failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
