"""Verify a rolled-back CAD placement probe and an independent Revit reread."""

import argparse
import hashlib
import json
import math
import sys

from cad_layout_preview import INPUT_LIMIT, contour_area, contour_area_tolerance, edges, fields, read_input
from revit_probe_record import require_no_changes

RECORD_LIMIT = 8 * INPUT_LIMIT


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def preview_payload(record):
    result = record["result"]
    fields(result, "status preview_json preview_sha256")
    payload = result["preview_json"]
    if (result["status"] != "proposed" or not isinstance(payload, str) or len(payload) > 2 * INPUT_LIMIT
            or sha256(payload) != result["preview_sha256"]):
        raise ValueError("The Revit preview bytes do not match a complete proposal digest.")
    preview = json.loads(payload)
    if (preview["kind"] != "revit-cad-layout-preview" or preview["status"] != "proposed"
            or preview["model_write_approved"] is not False or preview["proposal"]["unresolved"]
            or preview["provenance"]["status"] != "no_tags" or preview["existing_model_scan"]["status"] != "none_found"):
        raise ValueError("The preview did not qualify for the rollback probe.")
    return preview


def near(value, target, tolerance):
    return type(value) in (int, float) and math.isfinite(value) and abs(value - target) <= tolerance


def verify_measurements(preview, measurements):
    fields(measurements, "wall_face_offsets_mm door_cuts rooms")
    proposal = preview["proposal"]
    tolerance = proposal["tolerance_mm"]
    walls = {wall["id"]: wall for wall in proposal["walls"]}
    doors = {door["source_ids"][0]: door for door in proposal["doors"]}
    rooms = {room["source_ids"][0]: room for room in proposal["rooms"]}
    for name, expected in (("wall_face_offsets_mm", walls), ("door_cuts", doors), ("rooms", rooms)):
        if not isinstance(measurements[name], dict) or measurements[name].keys() != expected.keys():
            raise ValueError(f"The {name} receipt differs from the reviewed scope.")
    for identifier, wall in walls.items():
        offsets = measurements["wall_face_offsets_mm"][identifier]
        half = wall["width_mm"] / 2
        if (not isinstance(offsets, list) or len(offsets) != 2
                or any(not isinstance(side, list) or not side or
                       any(not near(value, expected, tolerance) or value * expected <= 0 for value in side)
                       for side, expected in zip(offsets, (-half, half)))):
            raise ValueError("A measured wall face differs from the reviewed wall.")
    for identifier, door in doors.items():
        cut = fields(measurements["door_cuts"][identifier], "elevation_mm samples")
        elevation = proposal["level_mm"] + preview["door_choices"][identifier]["cut_probe_height_mm"]
        depth = 0.375 * walls[door["host"]]["width_mm"]
        if not near(cut["elevation_mm"], elevation, 1e-6) or not isinstance(cut["samples"], list) or len(cut["samples"]) != 3:
            raise ValueError("The door cut receipt lacks measurements at the reviewed height and three depths.")
        for sample, expected in zip(cut["samples"], (-depth, 0, depth)):
            fields(sample, "lateral_mm span_mm")
            span = sample["span_mm"]
            if (not near(sample["lateral_mm"], expected, 1e-6) or not isinstance(span, list) or len(span) != 2
                    or any(not near(value, target, tolerance) for value, target in zip(span, door["span_mm"]))):
                raise ValueError("A measured door opening differs from the reviewed cut span.")
    for identifier, room in rooms.items():
        measurement = fields(measurements["rooms"][identifier], "finish_area_mm2 segment_count finish_edges_mm")
        sides = edges(room["boundary"])
        if (type(measurement["segment_count"]) is not int or measurement["segment_count"] != len(sides)
                or not near(measurement["finish_area_mm2"], contour_area(sides), contour_area_tolerance(sides, tolerance))
                or measurement["finish_area_mm2"] <= 0):
            raise ValueError("A measured room differs from the reviewed finish area or segment count.")
        actual = measurement["finish_edges_mm"]
        if (not isinstance(actual, list) or len(actual) != len(sides)
                or any(not isinstance(edge, list) or len(edge) != 2
                       or any(not isinstance(point, list) or len(point) != 2
                              or any(type(value) not in (int, float) or not math.isfinite(value) for value in point)
                              for point in edge)
                       for edge in actual)):
            raise ValueError("The room receipt lacks complete finish edges.")
        unmatched = list(actual)
        for start, end in sides:
            matches = [edge for edge in unmatched
                       if any(math.dist(start, first) <= tolerance and math.dist(end, last) <= tolerance
                              for first, last in (edge, edge[::-1]))]
            if len(matches) != 1:
                raise ValueError("The measured room finish edges differ from the reviewed outline.")
            unmatched.remove(matches[0])


def verify(preview_run, dry_run, fresh_run):
    before = require_no_changes(preview_run, "query")
    dry = require_no_changes(dry_run, "dry_run")
    fresh = require_no_changes(fresh_run, "query")
    if len({record["run_id"] for record in (before, dry, fresh)}) != 3:
        raise ValueError("Use separate Revit runs for the preview, rollback probe, and fresh query.")
    preview = preview_payload(before)
    preview_payload(fresh)
    if fresh["result"] != before["result"]:
        raise ValueError("The fresh Revit preview differs after rollback.")
    result = fields(dry["result"], "status rollback_verified preview_sha256 transient_element_count measurements")
    pieces = preview["placement_manifest"]["pieces"].values()
    count = len(pieces) + int(any(piece["kind"] == "separator" for piece in pieces))
    if (result["status"] != "probe_rolled_back" or result["rollback_verified"] is not True
            or result["preview_sha256"] != before["result"]["preview_sha256"]
            or type(result["transient_element_count"]) is not int or result["transient_element_count"] != count or not count):
        raise ValueError("The rollback probe does not match the reviewed preview and placement count.")
    verify_measurements(preview, result["measurements"])
    canonical = json.dumps(result["measurements"], ensure_ascii=True, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return {"status": "rollback_probe_verified", "preview_sha256": result["preview_sha256"],
            "dry_run_id": dry["run_id"], "fresh_run_id": fresh["run_id"], "measurements_sha256": sha256(canonical)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preview_run")
    parser.add_argument("dry_run")
    parser.add_argument("fresh_run")
    args = parser.parse_args(argv)
    try:
        records = [read_input(path, RECORD_LIMIT)[0] for path in (args.preview_run, args.dry_run, args.fresh_run)]
        print(json.dumps(verify(*records), indent=2))
    except (ValueError, TypeError, KeyError, OSError, AttributeError, OverflowError, RecursionError) as error:
        print(f"CAD rollback probe verification stopped: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
