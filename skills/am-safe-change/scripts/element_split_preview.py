"""Preview wall, column and floor split geometry from prepared local evidence."""

import argparse
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
import sys


MAX_BYTES = 2 * 1024 * 1024
MAX_SOURCES = 20
MAX_CUTS = 10
MAX_PIECES = 100
MAX_GEOMETRY = 2000
COLUMNS = ["Element ID", "Category", "Type ID", "Geometry"]
CATEGORIES = {"wall": "Wall", "architectural_column": "Architectural column",
              "structural_column": "Structural column", "floor": "Floor"}


class BlockedSplit(ValueError):
    """The geometry is understood, but a qualifying split could not be proved."""


def exact_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("The evidence repeats a JSON field.")
    return result


def fields(value, names):
    if not isinstance(value, dict) or value.keys() != names:
        raise ValueError("The evidence has missing or unsupported fields.")
    return value


def bounded_list(value, minimum, maximum, label):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"Supply {minimum} to {maximum} {label}.")
    return value


def number(value):
    if type(value) is int:
        value = Decimal(value)
    if type(value) is not Decimal or not value.is_finite() or value.copy_abs() > 1_000_000:
        raise ValueError("Use finite numeric coordinates within one million feet.")
    representation = value.as_tuple()
    if len(representation.digits) > 24 or abs(representation.exponent) > 32:
        raise ValueError("The coordinate precision exceeds the supported input size.")
    return Fraction(value)


def point(value):
    return tuple(map(number, bounded_list(value, 2, 2, "XY coordinates")))


def identifier(value):
    if (not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]{0,18}", value) is None
            or int(value) >= 2**63):
        raise ValueError("Use positive decimal string Revit IDs.")
    return value


def uid(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isprintable():
        raise ValueError("Use a printable source identity of at most 128 characters.")
    return value


def edges(vertices):
    return zip(vertices, vertices[1:] + vertices[:1])


def cross(a, b, p):
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def squared_distance(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b))


def twice_area(vertices):
    return sum(a[0] * b[1] - a[1] * b[0] for a, b in edges(vertices))


def clip(vertices, start, end, side):
    result = []
    for a, b in edges(vertices):
        da, db = cross(start, end, a) * side, cross(start, end, b) * side
        if da >= 0:
            result.append(a)
        if da * db < 0:
            ratio = da / (da - db)
            result.append(tuple(x + (y - x) * ratio for x, y in zip(a, b)))
    return result


def split_polygon(vertices, start, end, finite, minimum):
    distances = [cross(start, end, vertex) for vertex in vertices]
    if not min(distances) < 0 < max(distances):
        return None
    pieces = [clip(vertices, start, end, side) for side in (1, -1)]
    shared = sorted(set(pieces[0]) & set(pieces[1]))
    if len(shared) != 2:
        raise BlockedSplit("The cut does not form one shared floor seam.")
    if finite:
        length = squared_distance(start, end)
        low, high = sorted(sum((coord - a) * (b - a) for coord, a, b in zip(vertex, start, end))
                           for vertex in shared)
        if high <= 0 or low >= length:
            return None
        if low < 0 or high > length:
            raise BlockedSplit("The reviewed line does not span the full floor seam.")
    if any(len(part) < 3 or twice_area(part) <= 0 or any(
            squared_distance(a, b) < minimum ** 2 for a, b in edges(part)) for part in pieces):
        raise BlockedSplit("The cut creates an empty or undersized floor piece.")
    if sum(map(twice_area, pieces)) != twice_area(vertices):
        raise BlockedSplit("The split does not conserve the source floor area.")
    return pieces, shared


def floor_pieces(geometry, cuts, minimum):
    fields(geometry, {"elevation_ft", "boundary_xy_ft"})
    elevation = number(geometry["elevation_ft"])
    vertices = [point(row) for row in bounded_list(geometry["boundary_xy_ft"], 3, 128, "floor vertices")]
    if twice_area(vertices) < 0:
        vertices.reverse()
    # Each directed edge must support the whole loop. This also rejects crossed or repeated edges.
    if len(set(vertices)) != len(vertices) or any(
            cross(a, b, p) <= 0 for a, b in edges(vertices) for p in vertices if p not in (a, b)):
        raise BlockedSplit("Use a simple convex floor boundary with no holes or redundant vertices.")
    original = {"elevation_ft": elevation, "boundary_xy_ft": vertices, "area_sqft": twice_area(vertices) / 2}
    parts, seams = [vertices], []
    for cut in cuts:
        fields(cut, {"id", "uid", "kind", "start_xy_ft", "end_xy_ft"})
        if cut["kind"] not in ("grid", "line"):
            raise ValueError("Floor cuts need a straight grid or line.")
        start, end = point(cut["start_xy_ft"]), point(cut["end_xy_ft"])
        if squared_distance(start, end) < minimum ** 2:
            raise BlockedSplit("The reviewed cut has insufficient length.")
        next_parts, cut_seams = [], []
        next_vertices = 0
        for part in parts:
            split = split_polygon(part, start, end, cut["kind"] == "line", minimum)
            if split is None:
                next_parts.append(part)
                next_vertices += len(part)
            else:
                pieces, seam = split
                next_parts.extend(pieces)
                next_vertices += sum(map(len, pieces))
                cut_seams.append(seam)
            if len(next_parts) > MAX_PIECES or next_vertices > MAX_GEOMETRY:
                raise ValueError("Floor partition exceeds the geometry budget.")
        if not cut_seams:
            raise BlockedSplit("A reviewed cut does not cross the floor interior or repeats an existing seam.")
        parts = next_parts
        seams.append({"cut_id": cut["id"], "segments_xy_ft": cut_seams})
    return original, [dict(original, boundary_xy_ft=part, area_sqft=twice_area(part) / 2) for part in parts], seams


def vertical_pieces(geometry, cuts, minimum):
    fields(geometry, {"base_ft", "top_ft"})
    base, top = number(geometry["base_ft"]), number(geometry["top_ft"])
    elevations = []
    for cut in cuts:
        fields(cut, {"id", "uid", "kind", "elevation_ft"})
        if cut["kind"] != "level":
            raise ValueError("Wall and column cuts need reviewed levels.")
        elevations.append(number(cut["elevation_ft"]))
    if any(not base < level < top for level in elevations) or len(set(elevations)) != len(elevations):
        raise BlockedSplit("Use distinct interior cut elevations.")
    bounds = [base, *sorted(elevations), top]
    if any(high - low < minimum for low, high in zip(bounds, bounds[1:])):
        raise BlockedSplit("A split would create an undersized wall or column piece.")
    return {"base_ft": base, "top_ft": top}, [
        {"base_ft": low, "top_ft": high} for low, high in zip(bounds, bounds[1:])], []


def geometry_text(item):
    if "area_sqft" in item:
        return f"{float(item['area_sqft']):.9g} ft² at {float(item['elevation_ft']):.9g} ft"
    return f"{float(item['base_ft']):.9g} to {float(item['top_ft']):.9g} ft"


def preview(snapshot):
    fields(snapshot, {"kind", "frame", "host_identity", "minimum_piece_ft", "elements"})
    if snapshot["kind"] != "element-split-evidence" or snapshot["frame"] != "revit_host_internal_feet":
        raise ValueError("Use element split evidence in host internal feet.")
    host = fields(snapshot["host_identity"], {"project_uid", "path_sha256"})
    uid(host["project_uid"])
    if not isinstance(host["path_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", host["path_sha256"]) is None:
        raise ValueError("The source needs a saved-project path digest.")
    minimum = number(snapshot["minimum_piece_ft"])
    if not 0 < minimum <= 1:
        raise ValueError("Supply a positive minimum piece length of at most one foot.")

    before, after, warnings, findings = [], [], [], []
    used_ids, used_uids = set(), set()
    types, cuts_by_id, cuts_by_uid = {}, {}, {}
    piece_count = input_geometry = output_geometry = 0
    for item in bounded_list(snapshot["elements"], 1, MAX_SOURCES, "split source elements"):
        fields(item, {"id", "uid", "type_id", "type_uid", "category", "geometry", "cuts"})
        source, source_uid = identifier(item["id"]), uid(item["uid"])
        type_id, type_uid = identifier(item["type_id"]), uid(item["type_uid"])
        if types.setdefault(type_id, type_uid) != type_uid:
            raise ValueError("A type ID has conflicting source identities.")
        if source in used_ids or source_uid in used_uids:
            raise ValueError("Split source element identities must be unique.")
        used_ids.add(source)
        used_uids.add(source_uid)
        category = CATEGORIES[item["category"]]
        cuts = bounded_list(item["cuts"], 1, MAX_CUTS, "cuts per source")
        if (len({identifier(cut["id"]) for cut in cuts}) != len(cuts)
                or len({uid(cut["uid"]) for cut in cuts}) != len(cuts)):
            raise ValueError("Each source needs distinct cut identities.")
        for cut in cuts:
            if (cuts_by_id.setdefault(cut["id"], cut) != cut
                    or cuts_by_uid.setdefault(cut["uid"], cut) != cut):
                raise ValueError("A shared cut has conflicting snapshot evidence.")

        prefix = [source, category, type_id]
        if item["category"] == "floor":
            input_geometry += len(item["geometry"]["boundary_xy_ft"]) + 2 * len(cuts)
        else:
            input_geometry += 2 + len(cuts)
        if input_geometry > MAX_GEOMETRY:
            raise ValueError("The prepared sources exceed the batch geometry budget.")
        try:
            calculate = floor_pieces if item["category"] == "floor" else vertical_pieces
            original, pieces, seams = calculate(item["geometry"], cuts, minimum)
        except BlockedSplit as error:
            before.append(prefix + ["Prepared geometry"])
            after.append(prefix + ["Blocked"])
            warnings.append({"row": source, "text": str(error), "blocking": True})
            continue
        piece_count += len(pieces)
        output_geometry += sum(len(part.get("boundary_xy_ft", ())) or 2 for part in pieces)
        output_geometry += 2 * sum(len(seam["segments_xy_ft"]) for seam in seams)
        if piece_count > MAX_PIECES or output_geometry > MAX_GEOMETRY:
            raise ValueError("The proposed pieces exceed the batch geometry budget.")
        before.append(prefix + [geometry_text(original)])
        after.append(prefix + [" | ".join(geometry_text(part) for part in pieces)])
        findings.append({"id": source, "uid": source_uid, "type_id": type_id, "type_uid": type_uid,
                         "category": item["category"], "original": original, "pieces": pieces, "seams": seams,
                         "cut_identities": [{"id": cut["id"], "uid": cut["uid"]} for cut in cuts]})
    return {"kind": "change-preview", "title": "Candidate element splits", "keyColumn": "Element ID",
            "columns": COLUMNS, "changedColumns": ["Geometry"], "before": before, "after": after,
            "warnings": warnings, "blocked": bool(warnings), "review_only": True, "steps": [], "ownership_ids": [],
            "findings": findings, "notes": ["Geometry comes from the supplied snapshot.",
                "Native eligibility, retained identity, dependencies and parameter preservation require Revit verification."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("The input exceeds 2 MiB.")
        report = preview(json.loads(raw, parse_float=Decimal, object_pairs_hook=exact_object))
        report["basis_sha256"] = sha256(raw).hexdigest()
        encoded = json.dumps(report, ensure_ascii=True, allow_nan=False, default=float)
        if len(encoded) > MAX_BYTES:
            raise ValueError("The preview exceeds 2 MiB.")
    except (ArithmeticError, OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        print(f"Element split preview stopped. {error}", file=sys.stderr)
        return 2
    print(encoded)
    return int(report["blocked"])


if __name__ == "__main__":
    sys.exit(main())
