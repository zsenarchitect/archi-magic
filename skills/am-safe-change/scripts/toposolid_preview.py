# -*- coding: utf-8 -*-
"""Create an am-safe-change review artifact for a rollback-only Toposolid probe."""

from __future__ import print_function

import argparse
import io
import json
import sys

from toposolid_floor_shape import derive
from toposolid_grade_check import finite, load


ALIGNMENT_REASONS = {
    "ambiguous_vertex": "More than one terrain vertex is within the selected XY tolerance.",
    "missing_vertex": "No terrain vertex is within the selected XY tolerance.",
    "duplicate_request": "The same terrain vertex was selected more than once.",
    "outside_support": "The terrain vertex is outside the selected support.",
}


def coordinates(point):
    return ", ".join("{:.12g}".format(value) for value in point) if point is not None else ""


def review(title, columns, before, after, warnings, notes, ownership_ids, probe_input):
    blocked = bool(warnings)
    return {
        "preview": {
            "kind": "change-preview", "title": title, "keyColumn": columns[0], "columns": columns,
            "changedColumns": [columns[-1]], "before": before, "after": after, "warnings": warnings,
            "notes": ["Run only on an authorized disposable Revit fixture. The probe rolls back."] + notes,
            "blocked": blocked, "review_only": True, "steps": [],
            "ownership_ids": [] if blocked else ownership_ids,
        },
        "probe_input": None if blocked else probe_input,
    }


def alignment_review(snapshot):
    alignment = snapshot["alignment"]
    support, rows = alignment["support"], alignment["rows"]
    if not rows:
        raise ValueError("Supply an alignment readback containing at least one requested point.")
    before, after, warnings = [], [], []
    for row in rows:
        key = str(row["point"])
        old, proposed = row.get("before_ft"), row.get("after_ft")
        reason = row.get("reason")
        if reason:
            warnings.append({"row": key, "text": ALIGNMENT_REASONS[reason], "blocking": True})
        elif old is None or proposed is None:
            raise ValueError("A proposed alignment row is missing its measured or planned coordinates.")
        xy = coordinates(row["requested_xy_ft"])
        before.append([key, xy, coordinates(old)])
        after.append([key, xy, coordinates(proposed) if not reason else "Excluded"])
    if alignment["blocked"] != bool(warnings):
        raise ValueError("The recorded alignment block status disagrees with its rows.")
    ids = [snapshot["toposolid"]["id"]] if any(row.get("before_ft") != row.get("after_ft") for row in rows) else []
    return review("Review Toposolid alignment probe", ["Point", "Selected XY ft", "Vertex XYZ ft"],
                  before, after, warnings, [
                      "Toposolid {} to {} {}.".format(snapshot["toposolid"]["id"], support["kind"], support["id"]),
                      "Maximum distance to support: {} ft.".format(alignment["tolerance_ft"]),
                  ], ids, {"baseline": snapshot})


def floor_review(snapshot, offset, xy, area, elevation):
    if not all(finite(value) for value in (offset, xy, area, elevation)) or min(xy, area, elevation) < 0:
        raise ValueError("Supply a finite floor offset and finite nonnegative geometry tolerances.")
    columns = ["Row", "Target", "Result"]
    before = [["floor", snapshot["region"]["id"], "Terrain source"]]
    after = [["floor", snapshot["region"]["id"], "Candidate floor type {} on level {}, offset {} ft".format(
        snapshot["floor_type"]["id"], snapshot["level"]["id"], offset)]]
    warnings = []
    try:
        shape = derive(snapshot, offset)
    except ValueError as error:
        shape = None
        warnings.append({"row": "floor", "text": str(error), "blocking": True})
        after[0][-1] = "Blocked"
    if shape is not None:
        edits = (
            ("boundary", shape["boundary_ft"], "Set boundary vertex XYZ ft"),
            ("interior", shape["interior_ft"], "Insert interior vertex XYZ ft"),
            ("crease", shape["split_lines_xy_ft"], "Add grade break between XY points ft"),
        )
        for label, items, action in edits:
            for index, item in enumerate(items, 1):
                key = "{}:{}".format(label, index)
                location = " to ".join(coordinates(point) for point in item) if label == "crease" else coordinates(item)
                before.append([key, location, ""])
                after.append([key, location, action])
    return review("Review Toposolid floor conversion probe", columns, before, after, warnings, [
        "Maximum differences: {} ft XY, {} sq ft uncovered or extra, {} ft elevation.".format(xy, area, elevation),
        "The source terrain and its sketch are the reference for verification.",
    ], [], {"baseline": snapshot, "shape": shape, "floor_offset_ft": offset, "xy_tolerance_ft": xy,
            "area_tolerance_sqft": area, "elevation_tolerance_ft": elevation})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode")
    for mode in ("alignment", "floor"):
        command = modes.add_parser(mode)
        command.add_argument("snapshot")
        command.add_argument("preview")
        if mode == "floor":
            for option in ("floor-offset-ft", "xy-tolerance-ft", "area-tolerance-sqft", "elevation-tolerance-ft"):
                command.add_argument("--" + option, type=float, required=True)
    args = parser.parse_args()
    try:
        if args.mode == "alignment":
            artifact = alignment_review(load(args.snapshot))
        elif args.mode == "floor":
            artifact = floor_review(load(args.snapshot), args.floor_offset_ft, args.xy_tolerance_ft,
                                    args.area_tolerance_sqft, args.elevation_tolerance_ft)
        else:
            raise ValueError("Choose alignment or floor.")
        with io.open(args.preview, "w", encoding="ascii") as stream:
            stream.write(u"{}\n".format(json.dumps(artifact["preview"], indent=2, ensure_ascii=True)))
        print(json.dumps(artifact["probe_input"], ensure_ascii=True, allow_nan=False))
    except (IOError, KeyError, TypeError, ValueError, AttributeError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 1 if artifact["preview"]["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
