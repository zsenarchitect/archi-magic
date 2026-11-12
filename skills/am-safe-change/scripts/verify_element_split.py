"""Compare measured split pieces against the exact reviewed source and typed expectations."""

import argparse
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

from element_split_preview import (MAX_BYTES, MAX_PIECES, bounded_list, exact_object, fields, identifier,
                                   number, point, preview, squared_distance, uid)


GEOMETRY_TOLERANCE = Fraction(1, 1_000_000)
MAX_PARAMETERS = 10_000
# Autodesk BuiltInParameter IDs. Only reviewed constraints and derived geometry may change.
HOST_GEOMETRY = {"parameter:-1012805", "parameter:-1012806"}
COLUMN_GEOMETRY = {"parameter:-1001350", "parameter:-1001351", "parameter:-1001357", "parameter:-1001358"}
ADJUSTABLE = {
    "wall": HOST_GEOMETRY | {"parameter:-1001103", "parameter:-1001105", "parameter:-1001107",
                             "parameter:-1001108", "parameter:-1001109", "parameter:-1004005"},
    "architectural_column": HOST_GEOMETRY | COLUMN_GEOMETRY,
    "structural_column": HOST_GEOMETRY | COLUMN_GEOMETRY,
    "floor": HOST_GEOMETRY,
}


def read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("A readback input exceeds 2 MiB.")
    return json.loads(raw, parse_float=Decimal, object_pairs_hook=exact_object), sha256(raw).hexdigest()


def signed_id(value):
    return (isinstance(value, str) and re.fullmatch(r"0|-?[1-9][0-9]{0,18}", value) is not None
            and -(2**63) <= int(value) < 2**63)


def typed_value(storage, value):
    if storage == "string":
        return isinstance(value, str) and len(value) <= 8192
    if storage == "integer":
        return type(value) is int and -(2**31) <= value < 2**31
    if storage == "element_id":
        return signed_id(value)
    return storage == "double" and (type(value) is int or type(value) is Decimal and value.is_finite())


def parameters(rows):
    values = {}
    for row in bounded_list(rows, 1, MAX_PARAMETERS, "typed parameters per element"):
        fields(row, {"identity", "storage", "has_value", "value", "read_only"})
        key, storage, value = uid(row["identity"]), row["storage"], row["value"]
        if key.startswith("parameter:"):
            valid_key = signed_id(key.removeprefix("parameter:"))
        else:
            valid_key = key.startswith("shared:") and bool(key.removeprefix("shared:"))
        if (not valid_key or key in values or storage not in ("string", "integer", "element_id", "double")
                or type(row["has_value"]) is not bool or type(row["read_only"]) is not bool
                or (not typed_value(storage, value) if row["has_value"] else value is not None)):
            raise ValueError("The typed parameter inventory is ambiguous or unreadable.")
        values[key] = row
    return values


def footprint(value, category):
    if category == "floor":
        if value is not None:
            raise ValueError("Use the measured floor boundary for floor footprint comparisons.")
        return None
    low, high = map(point, bounded_list(value, 2, 2, "vertical footprint corners"))
    if any(a >= b for a, b in zip(low, high)):
        raise ValueError("The vertical element footprint is empty.")
    return low, high


def matching_geometry(category, planned, observed):
    if category != "floor":
        fields(observed, {"base_ft", "top_ft"})
        lower, upper = number(observed["base_ft"]), number(observed["top_ft"])
        return lower < upper and all(abs(value - planned[key]) <= GEOMETRY_TOLERANCE
                                     for value, key in ((lower, "base_ft"), (upper, "top_ft")))
    fields(observed, {"elevation_ft", "boundary_xy_ft"})
    vertices = [point(row) for row in bounded_list(observed["boundary_xy_ft"], 3, 128, "readback floor vertices")]
    expected = planned["boundary_xy_ft"]
    if len(vertices) != len(expected) or abs(number(observed["elevation_ft"]) - planned["elevation_ft"]) > GEOMETRY_TOLERANCE:
        return False
    tolerance_squared = GEOMETRY_TOLERANCE ** 2
    starts = [index for index, vertex in enumerate(vertices) if squared_distance(vertex, expected[0]) <= tolerance_squared]
    return len(starts) == 1 and any(
        all(squared_distance(vertices[(starts[0] + direction * index) % len(vertices)], vertex) <= tolerance_squared
            for index, vertex in enumerate(expected)) for direction in (-1, 1))


def reviewed_adjustments(approval, snapshot_sha, inventory_sha, expected, baseline):
    fields(approval, {"kind", "snapshot_sha256", "inventory_sha256", "piece_adjustments"})
    if (approval["kind"] != "element-split-readback-approval" or approval["snapshot_sha256"] != snapshot_sha
            or approval["inventory_sha256"] != inventory_sha):
        raise ValueError("The reviewed adjustments refer to different source evidence.")
    overrides = {}
    for row in bounded_list(approval["piece_adjustments"], 0, MAX_PIECES, "reviewed piece adjustments"):
        fields(row, {"source_id", "piece", "values"})
        source, index = identifier(row["source_id"]), row["piece"]
        if (source not in expected or type(index) is not int or not 1 <= index <= len(expected[source]["pieces"])
                or (source, index) in overrides or not isinstance(row["values"], dict) or not row["values"]):
            raise ValueError("An adjustment does not identify one reviewed piece.")
        allowed = ADJUSTABLE[expected[source]["category"]]
        changes = {}
        for key, value in row["values"].items():
            original = baseline[source].get(key)
            if (key not in allowed or original is None or (value is not None and not typed_value(original["storage"], value))):
                raise ValueError("Adjustments must be typed values for reviewed geometric parameters.")
            changes[key] = dict(original, value=value, has_value=value is not None)
        overrides[source, index] = changes
    return overrides


def verify(snapshot, snapshot_sha, inventory, inventory_sha, approval, measured):
    plan = preview(snapshot)
    if plan["blocked"]:
        raise ValueError("Use a complete, unblocked split preview basis.")
    expected = {item["id"]: item for item in plan["findings"]}
    fields(inventory, {"kind", "basis_sha256", "revit_version", "elements"})
    fields(measured, {"kind", "host_identity", "revit_version", "pieces"})
    if (inventory["kind"] != "element-split-source-inventory" or inventory["basis_sha256"] != snapshot_sha
            or measured["kind"] != "element-split-readback" or measured["host_identity"] != snapshot["host_identity"]
            or inventory["revit_version"] != "2026" or measured["revit_version"] != inventory["revit_version"]):
        raise ValueError("The source inventory and readback must belong to the same reviewed Revit project and version.")
    baseline, footprints = {}, {}
    total_parameters = 0
    for row in bounded_list(inventory["elements"], 1, len(expected), "source inventories"):
        source = identifier(row["id"])
        if source not in expected or source in baseline or uid(row["uid"]) != expected[source]["uid"]:
            raise ValueError("The source inventory does not match the split scope.")
        baseline[source] = parameters(row["parameters"])
        footprints[source] = footprint(row["footprint_xy_ft"], expected[source]["category"])
        total_parameters += len(baseline[source])
        if total_parameters > MAX_PARAMETERS:
            raise ValueError("The original parameter inventory exceeds its budget.")
    if baseline.keys() != expected.keys():
        raise ValueError("The source inventory is incomplete.")
    overrides = reviewed_adjustments(approval, snapshot_sha, inventory_sha, expected, baseline)

    errors, matches = [], []
    remaining = {source: set(range(1, len(row["pieces"]) + 1)) for source, row in expected.items()}
    source_by_uid = {item["uid"]: item["id"] for item in expected.values()}
    seen_ids, seen_uids = set(), set()
    total_parameters = 0
    for row in bounded_list(measured["pieces"], 0, MAX_PIECES, "measured pieces"):
        fields(row, {"source_id", "id", "uid", "type_id", "type_uid", "geometry", "parameters", "footprint_xy_ft"})
        source, piece, piece_uid = identifier(row["source_id"]), identifier(row["id"]), uid(row["uid"])
        if (source not in expected or piece in seen_ids or piece_uid in seen_uids
                or (piece in expected and (piece != source or piece_uid != expected[source]["uid"]))
                or (piece_uid in source_by_uid and source_by_uid[piece_uid] != piece)):
            raise ValueError("The readback repeats or substitutes a source or result identity.")
        seen_ids.add(piece)
        seen_uids.add(piece_uid)
        actual = parameters(row["parameters"])
        total_parameters += len(actual)
        if total_parameters > MAX_PARAMETERS:
            raise ValueError("The measured parameter inventory exceeds its budget.")
        item = expected[source]
        measured_footprint = footprint(row["footprint_xy_ft"], item["category"])
        if measured_footprint is not None and any(
                squared_distance(a, b) > GEOMETRY_TOLERANCE ** 2
                for a, b in zip(measured_footprint, footprints[source])):
            errors.append({"id": piece, "reason": "footprint_changed"})
        candidates = [index for index, geometry in enumerate(item["pieces"], 1)
                      if matching_geometry(item["category"], geometry, row["geometry"])]
        if len(candidates) != 1 or candidates[0] not in remaining[source]:
            errors.append({"id": piece, "reason": "unmatched_or_ambiguous_geometry"})
            continue
        index = candidates[0]
        remaining[source].remove(index)
        matches.append({"source_id": source, "piece": index, "result_id": piece})
        if (identifier(row["type_id"]), uid(row["type_uid"])) != (item["type_id"], item["type_uid"]):
            errors.append({"id": piece, "reason": "type_changed"})
        wanted = {**baseline[source], **overrides.get((source, index), {})}
        for key in sorted(wanted.keys() | actual.keys()):
            if wanted.get(key) != actual.get(key):
                reason = "missing_parameter" if key not in actual else (
                    "extra_parameter" if key not in wanted else "parameter_changed")
                errors.append({"id": piece, "reason": reason, "parameter": key})
    for source, indices in remaining.items():
        errors.extend({"source_id": source, "piece": index, "reason": "missing_piece"} for index in sorted(indices))
    return {"kind": "element-split-readback-comparison", "status": "mismatch" if errors else "matched",
            "matches": matches, "differences": errors,
            "checks": ["project", "source and result identities", "piece extents or floor boundaries",
                       "vertical footprints", "type", "typed instance parameters"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("snapshot", "inventory", "approval", "readback"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--approval-sha256", required=True)
    args = parser.parse_args()
    try:
        snapshot, snapshot_sha = read_json(args.snapshot)
        inventory, inventory_sha = read_json(args.inventory)
        approval, approval_sha = read_json(args.approval)
        if approval_sha != args.approval_sha256:
            raise ValueError("The approval digest does not match the reviewed adjustment file.")
        measured, measured_sha = read_json(args.readback)
        result = verify(snapshot, snapshot_sha, inventory, inventory_sha, approval, measured)
        result.update(approval_sha256=approval_sha, readback_sha256=measured_sha)
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False)
        if len(encoded) > MAX_BYTES:
            raise ValueError("The comparison exceeds 2 MiB.")
    except (ArithmeticError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        print(f"Element split readback stopped. {error}", file=sys.stderr)
        return 2
    print(encoded)
    return int(result["status"] != "matched")


if __name__ == "__main__":
    sys.exit(main())
