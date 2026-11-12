"""Compute reviewed plan dimension strings from resolved Revit witness evidence."""

import argparse
import hashlib
import json
import math
import re
import sys

from focus_view_preview import check_frame, dot, real, triple

DISTANCE_TOLERANCE = 0.001
ANGLE_TOLERANCE = 0.00001
MAX_INPUT = 2 * 1024 * 1024
MAX_WITNESSES = 64
MODES = ("grid", "overall", "opening")
COLUMNS = ["String", "Mode", "Status", "References", "Segments ft"]


def label(value, length=240):
    if not isinstance(value, str) or not 0 < len(value) <= length or not value.strip() or not value.isprintable():
        raise ValueError("Expected a bounded printable identity.")
    return value


def element_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        raise ValueError("Use a positive decimal Revit element ID.")
    return value


def difference(a, b):
    return [left - right for left, right in zip(a, b)]


def unit(vector, minimum=1e-9):
    length = math.sqrt(dot(vector, vector))
    if length <= minimum:
        raise ValueError("The line or direction is too short.")
    return [component / length for component in vector], length


def project(point, origin, axes):
    vector = difference(triple(point), origin)
    if abs(dot(vector, axes[2])) > DISTANCE_TOLERANCE:
        raise ValueError("The line or witness is outside the reviewed view plane.")
    return [dot(vector, axis) for axis in axes[:2]]


def projected_line(raw, origin, axes, minimum):
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError("Supply both line endpoints in the view plane.")
    start, end = (project(point, origin, axes) for point in raw)
    direction, length = unit(difference(end, start), minimum)
    return start, direction, length


def world_point(point, origin, axes):
    return [origin[i] + point[0] * axes[0][i] + point[1] * axes[1][i] for i in range(3)]


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def station(witness, start, axis, length, origin, axes, mode):
    point, direction, extent = projected_line(witness["trace"], origin, axes, DISTANCE_TOLERANCE)
    if abs(dot(axis, direction)) > math.sin(ANGLE_TOLERANCE):
        raise ValueError("The dimension line is not perpendicular to the witness.")
    relative = difference(point, start)
    denominator = cross(axis, direction)
    position = cross(relative, direction) / denominator
    witness_position = cross(relative, axis) / denominator
    if not -DISTANCE_TOLERANCE <= position <= length + DISTANCE_TOLERANCE:
        raise ValueError("The reviewed line does not span the witness.")
    if mode != "opening" and not -DISTANCE_TOLERANCE <= witness_position <= extent + DISTANCE_TOLERANCE:
        raise ValueError("The line misses the reviewed witness extent.")
    return position


def proposal(row, context):
    origin, axes, minimum, default_type = context
    mode = row["mode"]
    if mode not in MODES:
        raise ValueError("Choose a grid, overall, or opening dimension.")
    if row.get("unresolved"):
        diagnostics = row["unresolved"]
        if not isinstance(diagnostics, list) or not 1 <= len(diagnostics) <= MAX_WITNESSES:
            raise ValueError("The unresolved-reference list is invalid.")
        details = ["{}: {}".format(element_id(item["source_id"]), label(item["reason"])) for item in diagnostics]
        raise ValueError("; ".join(details))

    witnesses = row.get("witnesses")
    maximum = MAX_WITNESSES if mode == "grid" else 2
    if not isinstance(witnesses, list) or not 2 <= len(witnesses) <= maximum:
        raise ValueError("The string has an unsupported number of witnesses.")
    line = row["line"]
    start, axis, length = projected_line(line, origin, axes, minimum)
    ordered = []
    seen = set()
    for witness in witnesses:
        id_value = element_id(witness["source_id"])
        stable = label(witness["stable_ref"], 1024)
        if stable in seen:
            raise ValueError("A native reference appears more than once in the string.")
        seen.add(stable)
        try:
            position = station(witness, start, axis, length, origin, axes, mode)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("{}: {}".format(id_value, error)) from error
        ordered.append((position, id_value, label(witness["source_uid"]), stable))
    ordered.sort()
    positions = [item[0] for item in ordered]
    values = [right - left for left, right in zip(positions, positions[1:])]
    if any(value <= DISTANCE_TOLERANCE for value in values):
        raise ValueError("Witness stations coincide within the placement tolerance.")
    end = [value + direction * length for value, direction in zip(start, axis)]
    return {
        "type_id": default_type,
        "line": [world_point(point, origin, axes) for point in (start, end)],
        "witnesses": [
            {"station_ft": position, "source_id": id_value, "source_uid": source_uid, "stable_ref": stable}
            for position, id_value, source_uid, stable in ordered
        ],
        "segments_ft": values,
    }


def signature(refs, values, start, direction, extent, type_id):
    if tuple(refs[::-1]) < tuple(refs):
        refs, values = refs[::-1], values[::-1]
    return refs, values, start, direction, extent, type_id


def existing_signature(row, context):
    origin, axes, minimum, _ = context
    refs = row["references"]
    values = row["segments_ft"]
    if (not isinstance(refs, list) or not 2 <= len(refs) <= MAX_WITNESSES
            or len(set(label(item, 1024) for item in refs)) != len(refs)
            or not isinstance(values, list) or len(values) != len(refs) - 1
            or not all(real(value, DISTANCE_TOLERANCE, 2e6) > DISTANCE_TOLERANCE for value in values)):
        raise ValueError("The existing dimension inventory contains unresolved references or values.")
    if row.get("line") is not None:
        start, direction, extent = projected_line(row["line"], origin, axes, minimum)
    else:
        support = row["support"]
        start = project(support["point"], origin, axes)
        vector = triple(support["direction"])
        if abs(dot(unit(vector)[0], axes[2])) > math.sin(ANGLE_TOLERANCE):
            raise ValueError("An existing dimension has a direction outside the reviewed plane.")
        direction, _ = unit([dot(vector, axis) for axis in axes[:2]])
        extent = None
    return signature(refs, values, start, direction, extent, element_id(row["type_id"]))


def proposal_signature(item, context):
    origin, axes, minimum, _ = context
    start, direction, extent = projected_line(item["line"], origin, axes, minimum)
    refs = [witness["stable_ref"] for witness in item["witnesses"]]
    return signature(refs, item["segments_ft"], start, direction, extent, item["type_id"])


def compare(candidate, other):
    refs, values, start, axis, length, type_id = candidate
    old_refs, old_values, old_start, old_axis, old_length, old_type = other
    if not set(refs).intersection(old_refs) or abs(cross(axis, old_axis)) > math.sin(ANGLE_TOLERANCE):
        return None
    shift = difference(old_start, start)
    if abs(cross(axis, shift)) > DISTANCE_TOLERANCE:
        return None
    if (refs != old_refs or type_id != old_type
            or any(abs(a - b) > DISTANCE_TOLERANCE for a, b in zip(values, old_values))):
        return "conflict"
    if old_length is not None:
        low, high = sorted((dot(shift, axis), dot(shift, axis) + old_length * dot(old_axis, axis)))
        if abs(low) > DISTANCE_TOLERANCE or abs(high - length) > DISTANCE_TOLERANCE:
            return "conflict"
    return "match"


def compute(snapshot):
    if (snapshot.get("kind") != "plan-dimension-snapshot" or type(snapshot.get("version")) is not int
            or snapshot["version"] != 1 or snapshot.get("existing_complete") is not True):
        raise ValueError("Supply a complete reviewed dimension snapshot.")
    project, view = snapshot["project"], snapshot["view"]
    label(project["project_uid"])
    if not re.fullmatch(r"[0-9a-f]{64}", project["path_sha256"]):
        raise ValueError("The reviewed project path digest is invalid.")
    view_id = element_id(view["id"])
    label(view["uid"])
    label(snapshot["dimension_type"]["uid"])
    type_id = element_id(snapshot["dimension_type"]["id"])
    origin, axes = check_frame(view["frame"])
    axes = [unit(axis)[0] for axis in axes]
    minimum = max(DISTANCE_TOLERANCE, real(snapshot["short_curve_tolerance_ft"], 0, 1))
    context = (origin, axes, minimum, type_id)

    existing, requests = snapshot["existing"], snapshot["requests"]
    if (not isinstance(existing, list) or len(existing) > 500
            or not isinstance(requests, list) or not 1 <= len(requests) <= 20):
        raise ValueError("Use 1 to 20 dimension requests and at most 500 existing dimensions.")
    prior = [existing_signature(row, context) for row in existing]
    before, after, warnings, proposals, pending, seen_keys = [], [], [], {}, [], set()
    for row in requests:
        row_key, mode = label(row["key"]), label(row["mode"])
        if row_key in seen_keys:
            raise ValueError("Each preview row needs a distinct key.")
        seen_keys.add(row_key)
        try:
            item = proposal(row, context)
            candidate = proposal_signature(item, context)
            existing_matches = [compare(candidate, other) for other in prior]
            pending_matches = [compare(candidate, other) for other in pending]
            if ("conflict" in existing_matches or "conflict" in pending_matches
                    or existing_matches.count("match") > 1):
                raise ValueError("Conflicting dimensions share the reviewed references and placement.")
            refs = len(item["witnesses"])
            segments = ", ".join(format(value, ".6g") for value in item["segments_ft"])
            if "match" in existing_matches:
                status = "Already exists"
            elif "match" in pending_matches:
                status = "Coalesced"
            else:
                status = "Proposed"
                pending.append(candidate)
                proposals[row_key] = item
            result = [row_key, mode, status, refs, segments]
            if status == "Already exists":
                before.append(result.copy())
        except (KeyError, TypeError, ValueError) as error:
            warnings.append({"row": row_key, "type": "unresolved_dimension", "text": str(error), "blocking": True})
            result = [row_key, mode, "Blocked", None, None]
        after.append(result)

    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"),
                                        ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
    preview = {
        "kind": "change-preview",
        "title": "Review Revit plan dimensions",
        "keyColumn": COLUMNS[0],
        "columns": COLUMNS,
        "changedColumns": COLUMNS[2:],
        "before": before,
        "after": after,
        "warnings": warnings,
        "notes": ["Review only. Resolve blocked rows before proceeding."],
        "blocked": bool(warnings),
        "review_only": True,
        "basis_sha256": digest,
        "ownership_ids": [view_id] if proposals and not warnings else [],
        "steps": [],
    }
    return preview, {} if warnings else proposals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    args = parser.parse_args()
    try:
        with open(args.snapshot, "rb") as stream:
            raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("The saved dimension snapshot exceeds 2 MiB.")
        preview, _ = compute(json.loads(raw))
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(preview, indent=2, allow_nan=False))
    return 1 if preview["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
