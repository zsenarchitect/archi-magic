# -*- coding: utf-8 -*-
"""Compute and verify the focus-view preview outside Revit."""
from __future__ import print_function

import argparse
import io
import json
import os
import sys

LIMIT = 2 * 1024 * 1024
EPSILON = 1e-6
WORLD = [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]
ACTIONS = ("create_3d", "reuse_3d", "crop_plan")


def real(number, minimum=-1e6, maximum=1e6):
    if type(number) not in (int, float) or not minimum <= number <= maximum:
        raise ValueError("Coordinate or padding is out of range.")
    return number


def triple(value):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("Expected a three-axis value.")
    return [real(number) for number in value]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def check_frame(frame):
    if not isinstance(frame, list) or len(frame) != 4:
        raise ValueError("Expected origin and three view axes.")
    origin, x, y, z = [triple(item) for item in frame]
    axes = [x, y, z]
    cross = [x[1]*y[2]-x[2]*y[1], x[2]*y[0]-x[0]*y[2], x[0]*y[1]-x[1]*y[0]]
    if (any(abs(dot(a, b) - int(i == j)) > EPSILON for i, a in enumerate(axes) for j, b in enumerate(axes))
            or any(abs(a - b) > EPSILON for a, b in zip(cross, z))):
        raise ValueError("The view frame is not orthonormal and right-handed.")
    return origin, axes


def padded_box(points, padding, frame, old_depth=None):
    origin, axes = check_frame(frame)
    local = [[dot([p - o for p, o in zip(triple(point), origin)], axis) for axis in axes] for point in points]
    if not local:
        raise ValueError("Select at least one supported model element.")
    low = [min(p[i] for p in local) - padding for i in range(3)]
    high = [max(p[i] for p in local) + padding for i in range(3)]
    if old_depth is not None:
        low[2], high[2] = old_depth
    low, high = triple(low), triple(high)
    if not all(0 < b - a <= 10000 for a, b in zip(low, high)):
        raise ValueError("The proposed box is empty or too large.")
    return {"frame": frame, "min": low, "max": high}


def compute(snapshot):
    if snapshot["kind"] != "focus-view-snapshot":
        raise ValueError("Expected a focus view snapshot.")
    request = snapshot["request"]
    action = request["action"]
    if action not in ACTIONS:
        raise ValueError("Unknown focus view action.")
    padding = real(request["padding_ft"], 0.01, 100)
    selection = snapshot["selection"]
    if not isinstance(selection, list) or not 1 <= len(selection) <= 100:
        raise ValueError("Select one to 100 supported model elements.")
    if len({element["id"] for element in selection}) != len(selection):
        raise ValueError("The selection contains repeated IDs.")
    points = []
    for element in selection:
        corners = element["corners"]
        if len(corners) != 8:
            raise ValueError("A selected element lacks its eight box corners.")
        points.extend(corners)
    target = snapshot["target"]
    old = target.get("box")
    depth = None
    if action == "crop_plan":
        depth = [triple(old["min"])[2], triple(old["max"])[2]]
    box = padded_box(points, padding, old["frame"] if depth is not None else WORLD, depth)
    name = request["name"] if action == "create_3d" else target["name"]
    key = "new" if action == "create_3d" else target["id"]
    placements = target.get("placements") or []
    warnings = []
    if placements:
        warnings.append({"row": key, "text": "This view is on a sheet. Review model and annotation clipping.",
                         "blocking": False})
    return {"kind": "change-preview", "recipe": "focus-view", "title": "Focus Revit view",
            "keyColumn": "View ID", "columns": ["View ID", "View", "Active box", "Minimum ft", "Maximum ft"],
            "changedColumns": ["Active box", "Minimum ft", "Maximum ft"],
            "before": [[key, name, target.get("box_active"), old["min"] if old else None, old["max"] if old else None]],
            "after": [[key, name, True, box["min"], box["max"]]],
            "notes": ["Padding %.3f ft around %d selected element(s)." % (padding, len(selection)),
                      "Box containment and view visibility require separate checks."],
            "warnings": warnings, "blocked": False,
            "ownership_ids": [] if action == "create_3d" else [key],
            "steps": [{"action": action, "id": key}], "expected_dependent_categories": [],
            "basis": snapshot, "box": box}


def close_values(expected, actual):
    if isinstance(expected, list) and isinstance(actual, list):
        return len(expected) == len(actual) and all(close_values(a, b) for a, b in zip(expected, actual))
    return abs(real(expected) - real(actual)) <= EPSILON


def matches_box(expected, actual):
    return all(close_values(expected[field], actual[field]) for field in ("frame", "min", "max"))


def check_result(plan, actual):
    if actual.get("status") != "ok" or actual.get("box_active") is not True:
        raise ValueError("The view readback did not report an active box.")
    basis = plan["basis"]
    if (actual["document"] != basis["document"] or actual["selection_ids"] != [row["id"] for row in basis["selection"]]
            or actual["active_view_uid"] != basis["active_view_uid"]):
        raise ValueError("The document, active view, or selection changed.")
    if json.loads(actual.get("selection_json", "null")) != basis["selection"]:
        raise ValueError("The selected element identity or bounds changed. Read and review the selection again.")
    if basis["request"]["action"] != "create_3d":
        if actual["view_id"] != basis["target"]["id"] or actual["view_uid"] != basis["target"]["uid"]:
            raise ValueError("The readback targets another view.")
    else:
        if (not actual.get("view_uid") or not actual.get("view_id")
                or json.loads(actual["name_json"]) != basis["request"]["name"]):
            raise ValueError("The new view identity or name is unavailable.")
    if not matches_box(plan["box"], actual["box"]) or actual.get("view_range") != basis["target"].get("view_range"):
        raise ValueError("The saved box or plan view range differs from the preview.")


def load(path):
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("The focus view file exceeds 2 MiB.")
    return json.loads(raw)


def unwrap(data):
    if "result" in data:
        data = data["result"]
    if not isinstance(data, dict):
        raise ValueError("The runtime result is unavailable.")
    return data


def read_basis(data):
    data = unwrap(data)
    if data.get("status") != "ok":
        raise ValueError("The focus view snapshot failed.")
    return json.loads(data["snapshot_json"])


def verify_changes(plan, data):
    actual = unwrap(data)
    check_result(plan, actual)
    change = data.get("changes") or {}
    create = plan["basis"]["request"]["action"] == "create_3d"
    categories = change.get("by_category")
    if (change.get("added_count") != int(create) or change.get("deleted_count") != 0
            or change.get("other_documents") or type(change.get("modified_count")) is not int
            or change["modified_count"] < 0 or not isinstance(categories, dict)
            or sum(categories.values()) != change["modified_count"] + int(create)
            or (not create and change["modified_count"] < 1)):
        raise ValueError("Dry-run change telemetry does not establish the intended view edit.")
    added = change.get("added_ids", [row["id"] for row in change.get("added", [])])
    if create and [str(i) for i in added] != [actual["view_id"]]:
        raise ValueError("Dry run did not identify the one created view.")
    modified = change.get("modified", [])
    if (len(modified) != change["modified_count"]
            or any(str(row["id"]) != actual["view_id"] for row in modified)):
        raise ValueError("Dry-run modified IDs are incomplete or include another element. Review the full run.")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
    import side_effects
    shown = dict(plan)
    adjusted = {"modified_count": change["modified_count"], "modified_by_category": dict(categories)}
    category = actual["category_json"]
    category = json.loads(category)
    if create:
        shown["steps"] = []
        if adjusted["modified_by_category"].get(category, 0) < 1:
            raise ValueError("The created view category is missing from the dry run.")
        adjusted["modified_by_category"][category] -= 1
        if not adjusted["modified_by_category"][category]:
            del adjusted["modified_by_category"][category]
        print("One view created in the dry run.")
    summary = side_effects.summarize(shown, adjusted, [category])
    print(side_effects.text_table(summary))
    if summary["unexpected"] or summary["warnings"]:
        raise ValueError("Review the reported side effects before proceeding.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compute", "baseline", "compare", "side-effects"))
    parser.add_argument("input")
    parser.add_argument("second")
    args = parser.parse_args()
    try:
        if args.command == "compute":
            plan = compute(read_basis(load(args.input)))
            with io.open(args.second, "w", encoding="utf-8") as output:
                output.write(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            plan = load(args.input)
            actual = load(args.second)
            if args.command == "compare":
                check_result(plan, unwrap(actual))
                print("Focus view readback matches the preview.")
            elif args.command == "baseline":
                if read_basis(actual) != plan["basis"]:
                    raise ValueError("The original view or selection state changed.")
                print("The original view and selection state match the preview baseline.")
            else:
                verify_changes(plan, actual)
    except (ValueError, KeyError, TypeError, IOError, AttributeError, OverflowError) as error:
        print("Focus view stopped: %s" % error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
