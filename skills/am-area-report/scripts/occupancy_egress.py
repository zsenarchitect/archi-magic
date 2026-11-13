"""Review occupant loads and measured egress widths from a scoped room snapshot."""

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import stat
import sys

LIMIT = 2 * 1024 * 1024
AREA_UNITS = {"ft2": Fraction(1), "m2": Fraction("0.09290304")}
WIDTH_UNITS = {"mm": Fraction(1), "in": Fraction("25.4")}


class ReviewError(ValueError):
    """An input cannot support the requested calculation."""


def bounded_decimal(token):
    if len(token) > 48:
        raise ReviewError("Number exceeds the supported precision.")
    number = Decimal(token)
    if not number.is_finite() or abs(number.as_tuple().exponent) > 36 or abs(number) > 10**9:
        raise ReviewError("Number is outside the supported range.")
    return number


def positive(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        value = bounded_decimal(value)
    if type(value) is not Decimal or value <= 0:
        raise ReviewError("Expected a positive decimal.")
    return Fraction(value)


def unique_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ReviewError("Duplicate JSON field.")
        fields[key] = value
    return fields


def read_input(path):
    if str(path).startswith(("//", "\\\\")) or not stat.S_ISREG(path.lstat().st_mode):
        raise ReviewError("Use regular local input files.")
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ReviewError("Input exceeds 2 MiB.")
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_fields,
                      parse_float=bounded_decimal, parse_int=bounded_decimal, parse_constant=bounded_decimal)
    return data, hashlib.sha256(raw).hexdigest()


def label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise ReviewError("Expected a nonempty label of at most 120 characters.")
    return value.strip()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        raise ReviewError("Expected a positive Revit ID as decimal text.")
    return value


def rows(values, cap):
    if not isinstance(values, list) or len(values) > cap:
        raise ReviewError("Invalid or excessive list.")
    return values


def width_mm(value):
    return positive(value["value"]) * WIDTH_UNITS[value["unit"]]


def display_mm(value):
    with localcontext() as context:
        context.prec = 60
        return format(Decimal(value.numerator) / Decimal(value.denominator), ".6f")


def room_loads(snapshot, criteria):
    if snapshot["kind"] != "occupancy-room-snapshot" or snapshot["area_basis"] != "net_room":
        raise ReviewError("Use a scoped net room snapshot.")
    identifier(snapshot["phase_id"])
    levels = [identifier(value) for value in rows(snapshot["level_ids"], 20)]
    if not levels or len(levels) != len(set(levels)):
        raise ReviewError("Snapshot level scope is empty or duplicated.")
    floors = {level: {"level_id": level, "known_occupants": 0, "placed_rooms": 0,
                      "excluded_rooms": 0, "unresolved_rooms": 0} for level in levels}
    factors = {}
    for factor in rows(criteria["occupancy_factors"], 300):
        if factor["basis"] != "net_room":
            raise ReviewError("Each factor must explicitly use the net room area basis.")
        key = label(factor["occupancy"]).casefold()
        if key in factors:
            raise ReviewError("Duplicate normalized occupancy key.")
        factors[key] = (positive(factor["area_per_person"]), AREA_UNITS[factor["unit"]], label(factor["source"]))

    rooms = {}
    source = rows(snapshot["rooms"], 3000)
    if snapshot["room_count"] != len(source):
        raise ReviewError("Snapshot room count disagrees with the room inventory.")
    for item in source:
        room_id = identifier(item["id"])
        level = identifier(item["level_id"])
        if room_id in rooms or level not in floors:
            raise ReviewError("Duplicate room ID or room outside snapshot level scope.")
        floor = floors[level]
        status = item["status"]
        room = {"room_id": room_id, "level_id": level, "state": status, "occupants": None}
        rooms[room_id] = room
        if status in ("not_placed", "not_enclosed", "redundant"):
            if item["area_ft2"] != 0:
                raise ReviewError("An excluded room has a nonzero area.")
            floor["excluded_rooms"] += 1
            continue
        if status != "placed":
            raise ReviewError("Unrecognized room status.")
        floor["placed_rooms"] += 1
        area = positive(item["area_ft2"])
        use = item["occupancy"]
        key = label(use).casefold() if isinstance(use, str) and use.strip() else None
        factor = factors.get(key)
        if factor is None:
            floor["unresolved_rooms"] += 1
            room["state"] = "occupancy_unresolved"
            continue
        area_per_person, conversion, evidence = factor
        ratio = area * conversion / area_per_person
        occupants = -(-ratio.numerator // ratio.denominator)
        if occupants > 10**9:
            raise ReviewError("Calculated room load exceeds the supported range.")
        room.update(state="calculated", occupants=occupants, occupancy=key, factor_source=evidence)
        floor["known_occupants"] += occupants
    for floor in floors.values():
        floor["complete"] = bool(floor["placed_rooms"] and not floor["excluded_rooms"]
                                 and not floor["unresolved_rooms"])
    return rooms, floors


def review_widths(criteria, rooms, floors):
    segments = {}
    for item in rows(criteria["segments"], 1000):
        segment_id = identifier(item["id"])
        if segment_id in rooms:
            raise ReviewError("An egress segment ID belongs to a room in the reviewed snapshot.")
        if segment_id in segments or item["kind"] not in ("door", "stair"):
            raise ReviewError("Duplicate segment ID or unsupported segment kind.")
        measurement = item["measurement"]
        if measurement["basis"] != "clear_width":
            raise ReviewError("Use a measured clear width for each segment.")
        segments[segment_id] = {
            "segment_id": segment_id, "kind": item["kind"],
            "measured": width_mm(measurement), "measurement_source": label(measurement["source"]),
            "minimum": width_mm(item["minimum"]), "per_person": width_mm(item["width_per_person"]),
            "criteria_source": label(item["criteria_source"]),
        }
    assigned = defaultdict(int)
    loads = defaultdict(int)
    known_routes = set()
    references = 0
    for route in rows(criteria["routes"], 3000):
        room_id = identifier(route["room_id"])
        path = tuple(identifier(value) for value in rows(route["segment_ids"], 20))
        references += len(path)
        if references > 30000:
            raise ReviewError("Route segment reference limit exceeded.")
        if (room_id not in rooms or rooms[room_id]["state"] not in ("calculated", "occupancy_unresolved")
                or not path or len(set(path)) != len(path) or not all(value in segments for value in path)
                or (room_id, path) in known_routes):
            raise ReviewError("Invalid, unknown or duplicate route.")
        known_routes.add((room_id, path))
        count = positive(route["occupants"])
        if count.denominator != 1 or count > 10**9:
            raise ReviewError("Route allocation must be a positive whole number.")
        assigned[room_id] += count.numerator
        for segment_id in path:
            loads[segment_id] += count.numerator
    if set(segments) != set(loads):
        raise ReviewError("Each segment must occur on a reviewed route.")
    allocations = []
    for room_id, room in rooms.items():
        if room["state"] in ("calculated", "occupancy_unresolved"):
            allocations.append({"room_id": room_id, "assigned": assigned[room_id],
                                "expected": room["occupants"], "complete": assigned[room_id] == room["occupants"]})
    complete = bool(allocations and all(row["complete"] for row in allocations)
                    and all(floor["complete"] for floor in floors.values()))
    comparisons = []
    for segment_id, segment in segments.items():
        measured = segment.pop("measured")
        required = max(segment.pop("minimum"), loads[segment_id] * segment.pop("per_person"))
        margin = measured - required
        segment.update(assigned_occupants=loads[segment_id], measured_mm=display_mm(measured),
                       required_mm=display_mm(required) if complete else None,
                       margin_mm=display_mm(margin) if complete else None,
                       comparison=("below_supplied_width" if margin < 0 else "meets_supplied_width")
                       if complete else "incomplete_allocation")
        comparisons.append(segment)
    return allocations, comparisons, complete


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("criteria", type=Path)
    args = parser.parse_args(argv)
    try:
        snapshot, digest = read_input(args.snapshot)
        criteria, criteria_digest = read_input(args.criteria)
        if criteria["snapshot_sha256"] != digest:
            raise ReviewError("Criteria do not match the exact room snapshot.")
        rooms, floors = room_loads(snapshot, criteria)
        allocations, comparisons, complete = review_widths(criteria, rooms, floors)
        report = {"kind": "occupancy-egress-review", "snapshot_sha256": digest, "criteria_sha256": criteria_digest,
                  "phase_id": snapshot["phase_id"], "rooms": list(rooms.values()), "floors": list(floors.values()),
                  "allocations": allocations, "segments": comparisons, "complete_width_inputs": complete,
                  "scope": "Comparison to supplied criteria and measured widths for the reviewed routes only."}
        output = json.dumps(report, ensure_ascii=True, indent=2)
        if len(output) > LIMIT:
            raise ReviewError("Report exceeds 2 MiB.")
        print(output)
    except ReviewError as exc:
        print("Occupancy review failed. " + str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, InvalidOperation, KeyError, TypeError, AttributeError, RecursionError):
        print("Occupancy review failed. Check the input shape, values, units, and encoding.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
