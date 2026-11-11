# -*- coding: utf-8 -*-
"""Plan creation of named Revit levels and straight grid lines."""
from __future__ import print_function

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import io
import json
import math
import re
import sys

try:
    TEXT = basestring
except NameError:
    TEXT = str

MAX_ROWS = 500
MAX_EXISTING = 10000
MAX_SNAPSHOT = 2 * 1024 * 1024
TOLERANCE = 1e-6
FEET = {"mm": Decimal("1") / Decimal("304.8"), "m": Decimal("1000") / Decimal("304.8"), "ft": Decimal(1)}
DECIMAL = re.compile(r"-?[0-9]{1,12}(?:\.[0-9]{1,6})?\Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def number(value):
    text = str(value)
    if not DECIMAL.match(text) or len(text.replace("-", "").replace(".", "")) > 12:
        raise ValueError("Use finite decimal distances with at most 12 digits and six decimal places.")
    return Decimal(text)


def feet(value, units):
    result = value * FEET[units]
    if abs(result) > 30000:
        raise ValueError("A datum coordinate exceeds 30,000 internal feet.")
    return float(result)


def name_of(value):
    if (not isinstance(value, TEXT) or not value.strip() or value != value.strip()
            or len(value) > 100 or any(ord(char) < 32 for char in value)):
        raise ValueError("Use a datum name of 1 to 100 characters without control characters.")
    return value


def expand_intervals(value, remaining):
    if not isinstance(value, TEXT) or len(value) > 2500:
        raise ValueError("Supply a comma-separated spacing string of up to 2,500 characters.")
    pieces = value.replace(u"\u00d7", "x").lower().split(",")
    if not 1 <= len(pieces) <= 100:
        raise ValueError("Use at most 100 spacing terms per grid run.")
    for piece in pieces:
        term = piece.strip()
        tokens = term.split("x")
        if len(tokens) == 1:
            count, distance = 1, number(tokens[0])
        elif len(tokens) == 2 and re.match(r"[1-9][0-9]{0,2}\Z", tokens[0].strip()):
            count, distance = int(tokens[0]), number(tokens[1].strip())
        else:
            raise ValueError("Use a distance or a count x distance spacing term.")
        if distance <= 0 or count > remaining:
            raise ValueError("Spacing must be positive and the expanded request may contain at most 500 datums.")
        remaining -= count
        for _ in range(count):
            yield distance


def requested_rows(request):
    if (not isinstance(request, dict) or set(request) - {"units", "levels", "grids"}
            or request.get("units") not in FEET):
        raise ValueError("Supply explicit units mm, m, or ft and the level or grid lists.")
    if len(canonical(request)) > 64 * 1024:
        raise ValueError("The datum request exceeds 64 KiB.")
    units = request["units"]
    levels, grids = request.get("levels", []), request.get("grids", [])
    if not isinstance(levels, list) or len(levels) > MAX_ROWS or not isinstance(grids, list) or len(grids) > 20:
        raise ValueError("Use at most 500 level rows and 20 grid runs.")
    output, previous = [], None
    for item in levels:
        if not isinstance(item, dict) or set(item) not in ({"name", "elevation"}, {"name", "spacing"}):
            raise ValueError("Each level needs a name and either elevation or spacing.")
        if "elevation" in item:
            elevation = number(item["elevation"])
        else:
            spacing = number(item["spacing"])
            if previous is None or spacing <= 0:
                raise ValueError("Start levels with an elevation and use positive relative spacing.")
            elevation = previous + spacing
            if feet(elevation, units) == feet(previous, units):
                raise ValueError("Two spaced levels collapse to the same Revit elevation.")
        output.append({"kind": "level", "name": name_of(item["name"]), "geometry": feet(elevation, units)})
        previous = elevation
    for run in grids:
        if (not isinstance(run, dict) or set(run) != {"axis", "start", "extent", "spacing", "names"}
                or run["axis"] not in ("X", "Y")):
            raise ValueError("A grid run needs axis X or Y, start, extent, spacing and names.")
        extent, names = run["extent"], run["names"]
        if not isinstance(extent, list) or len(extent) != 2 or not isinstance(names, list):
            raise ValueError("Supply two perpendicular extent coordinates and the exact grid names.")
        remaining = MAX_ROWS - len(output)
        if not 2 <= len(names) <= remaining:
            raise ValueError("Supply the start grid name and one name per spacing interval within the datum budget.")
        edges = sorted(feet(number(coord), units) for coord in extent)
        if edges[0] == edges[1]:
            raise ValueError("A grid needs distinct perpendicular extent endpoints.")
        pos = number(run["start"])
        coordinates = [feet(pos, units)]
        for spacing in expand_intervals(run["spacing"], remaining - 1):
            pos += spacing
            coord = feet(pos, units)
            if coord <= coordinates[-1]:
                raise ValueError("Two grids collapse to the same Revit coordinate.")
            coordinates.append(coord)
        if len(names) != len(coordinates):
            raise ValueError("Supply one grid name for the start and one for each spacing interval.")
        for name, coord in zip(names, coordinates):
            points = [[coord, edge, 0.0] if run["axis"] == "X" else [edge, coord, 0.0] for edge in edges]
            output.append({"kind": "grid", "name": name_of(name), "geometry": points})
    if not output:
        raise ValueError("Supply at least one level or grid run.")
    return output


def geometry_matches(kind, actual, expected):
    if actual is None or expected is None:
        return actual is None and expected is None
    def numeric(value):
        return (not isinstance(value, bool) and isinstance(value, (int, float))
                and not math.isnan(value) and not math.isinf(value))
    if kind == "level":
        return numeric(actual) and numeric(expected) and abs(actual - expected) <= TOLERANCE
    for points in (actual, expected):
        if (not isinstance(points, list) or len(points) != 2
                or any(not isinstance(point, list) or len(point) != 3
                       or not all(numeric(coord) for coord in point) for point in points)):
            return False
    return all(abs(a - b) <= TOLERANCE
               for left, right in zip(sorted(actual), sorted(expected)) for a, b in zip(left, right))


def location(kind, geometry):
    if geometry is None:
        return "Curved or multi-segment grid"
    if kind == "level":
        return "%.6f ft project elevation" % geometry
    return u" to ".join("(%.6f, %.6f, %.6f) ft" % tuple(point) for point in geometry)


def compute(snapshot):
    if len(canonical(snapshot)) > MAX_SNAPSHOT:
        raise ValueError("The datum snapshot exceeds 2 MiB.")
    inventory = snapshot["existing"]
    if not isinstance(inventory, list) or len(inventory) > MAX_EXISTING:
        raise ValueError("The datum name inventory is incomplete or too large.")
    request = snapshot["request"]
    rows = requested_rows(request)
    requested = Counter((row["kind"], row["name"].lower()) for row in rows)
    existing, geometry_names = {}, {}
    for row in inventory:
        name_key = row["kind"], row["name"].lower()
        existing.setdefault(name_key, []).append(row)
        if row["geometry"] is not None:
            geometry_names.setdefault((row["kind"], canonical(row["geometry"])), []).append(row["name"])

    before, after, warnings, steps = [], [], [], []
    for index, item in enumerate(rows, 1):
        kind, name = item["kind"], item["name"]
        row_id = "%s:%d" % (kind, index)
        key = kind, name.lower()
        requested_location = location(kind, item["geometry"])
        candidates = existing.get(key, [])
        if requested[key] > 1:
            row = [row_id, kind, name, requested_location, "", "Blocked duplicate request"]
            before.append(row)
            after.append(row)
            warnings.append({"row": row_id, "text": "The request repeats this datum name.", "blocking": True})
        elif candidates:
            details = u"; ".join(u"%s [ID %s] %s" %
                                 (datum["name"], datum["id"], location(kind, datum["geometry"])) for datum in candidates)
            differs = any(not geometry_matches(kind, datum["geometry"], item["geometry"]) for datum in candidates)
            row = [row_id, kind, name, requested_location, details, "Skipped existing name"]
            before.append(row)
            after.append(row)
            warning = "Existing name. The requested and existing locations differ." if differs else "Existing name. No duplicate will be created."
            warnings.append({"row": row_id, "text": warning, "blocking": False})
        else:
            after.append([row_id, kind, name, requested_location, "", "Create"])
            same = geometry_names.get((kind, canonical(item["geometry"])), [])
            if same:
                warnings.append({"row": row_id, "text": u"Same location as " + u", ".join(same),
                                 "blocking": False})
            geometry_names.setdefault((kind, canonical(item["geometry"])), []).append(name)
            steps.append(item)
    blocked = any(warning["blocking"] for warning in warnings)
    basis = digest(snapshot)
    shown = {
        "kind": "change-preview", "title": "Create levels and grids", "keyColumn": "Row",
        "columns": ["Row", "Datum", "Name", "Requested location", "Existing", "Outcome"],
        "changedColumns": [], "before": before, "after": after,
        "warnings": warnings, "blocked": blocked, "review_only": True, "steps": [], "ownership_ids": [],
        "notes": ["Relative spacing uses the requested coordinates, including rows with existing names.",
                  "No datums will be created." if not steps or blocked else "%d new datums." % len(steps)],
        "datum_basis": basis,
    }
    return {"preview": shown, "plan": {"recipe": "datum_creation", "project": snapshot["project"],
                                     "request": request, "basis": basis, "steps": [] if blocked else steps}}


def runtime_result(record):
    result = record.get("result", record)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValueError("Use the full successful Revit run result.")
    return result


def snapshot_from(record):
    return json.loads(runtime_result(record)["snapshot_json"])


def verify_contents(plan, record):
    receipt = json.loads(runtime_result(record)["datums_json"])
    if receipt["project"] != plan["project"] or len(receipt["datums"]) != len(plan["steps"]):
        raise ValueError("The datum readback is from a different project or has the wrong count.")
    ids, uids = set(), set()
    for actual, step in zip(receipt["datums"], plan["steps"]):
        if (not isinstance(actual["id"], TEXT) or not re.match(r"[1-9][0-9]{0,18}\Z", actual["id"])
                or int(actual["id"]) >= 2 ** 63 or actual["id"] in ids
                or not actual["uid"] or actual["uid"] in uids
                or actual["kind"] != step["kind"] or actual["name"] != step["name"]
                or not geometry_matches(step["kind"], actual["geometry"], step["geometry"])):
            raise ValueError("The created datum identity, name, or geometry differs from the plan.")
        ids.add(actual["id"])
        uids.add(actual["uid"])
    return receipt, ids


def verify_dry_run(plan, record):
    _, ids = verify_contents(plan, record)
    changes = record.get("changes") or {}
    added = changes.get("added_ids")
    if added is None:
        added = [item["id"] for item in changes.get("added", [])]
    if (changes.get("added_count") != len(ids) or {str(item) for item in added} != ids
            or changes.get("modified_count") != 0 or changes.get("deleted_count") != 0
            or changes.get("other_documents")):
        raise ValueError("Dry-run telemetry differs from the reviewed datum additions.")


def load(path):
    with io.open(path, encoding="utf-8-sig") as source:
        return json.load(source)


def write(path, data):
    with io.open(path, "w", encoding="utf-8") as output:
        output.write(canonical(data) + u"\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compute", "baseline", "side-effects", "compare"))
    parser.add_argument("record")
    parser.add_argument("artifact")
    parser.add_argument("--preview")
    parser.add_argument("--kept-receipt")
    args = parser.parse_args()
    try:
        record = load(args.record)
        if args.command == "compute":
            artifact = compute(snapshot_from(record))
            write(args.artifact, artifact)
            if args.preview:
                write(args.preview, artifact["preview"])
            return 1 if artifact["preview"]["blocked"] else 0
        plan = load(args.artifact)["plan"]
        if args.command == "baseline":
            if digest(snapshot_from(record)) != plan["basis"]:
                raise ValueError("The project datum inventory changed after preview.")
        elif args.command == "side-effects":
            verify_dry_run(plan, record)
        else:
            current, _ = verify_contents(plan, record)
            if not args.kept_receipt or current != verify_contents(plan, load(args.kept_receipt))[0]:
                raise ValueError("The fresh read does not match the actual kept-run identities and values.")
        print("Datum %s check passed." % args.command)
    except (IOError, KeyError, TypeError, ValueError, AttributeError) as error:
        print("Datum creation stopped: %s" % error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
