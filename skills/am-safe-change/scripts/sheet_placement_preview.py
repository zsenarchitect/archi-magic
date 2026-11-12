"""Plan existing Revit view placements and verify measured sheet-space layout."""

import argparse
from collections import Counter, defaultdict
import hashlib
import math
from pathlib import Path
import sys

from sheet_creation_preview import SHA256, bounded, element_id, encode, fields, load, text, token, uid
from view_duplicate_preview import read_payload

MAX_ROWS = 50
MAX_GRAPHICS = 1000
TOLERANCE_FT = 1e-6
KINDS = ("view", "legend", "schedule")


def coordinates(value, length):
    if (not isinstance(value, list) or len(value) != length
            or any(type(item) not in (int, float) or not -1000 <= item <= 1000 for item in value)):
        raise ValueError("Use finite, bounded paper-space coordinates in feet.")
    return value


def rectangle(value):
    result = coordinates(value, 4)
    if any(result[axis] >= result[axis + 2] for axis in (0, 1)):
        raise ValueError("A paper-space rectangle has zero or negative extent.")
    return result


def fits(inner, outer, clearance):
    return all(inner[axis] - outer[axis] >= clearance - TOLERANCE_FT
               and outer[axis + 2] - inner[axis + 2] >= clearance - TOLERANCE_FT for axis in (0, 1))


def overlap(left, right, clearance):
    return not any(right[axis] - left[axis + 2] >= clearance - TOLERANCE_FT
                   or left[axis] - right[axis + 2] >= clearance - TOLERANCE_FT for axis in (0, 1))


def same_point(first, second):
    return all(math.isclose(x, y, rel_tol=0, abs_tol=TOLERANCE_FT) for x, y in zip(first, second))


def request_rows(request):
    fields(request, ("placements", "sheets", "clearance_ft"))
    clearance = request["clearance_ft"]
    if type(clearance) not in (int, float) or not 0 <= clearance <= 1:
        raise ValueError("Use a clearance between zero and one foot.")
    rows, seen = [], set()
    for item in bounded(request["placements"], MAX_ROWS):
        fields(item, ("view_id", "sheet_ids", "point_ft"))
        view_id = element_id(item["view_id"])
        point = coordinates(item["point_ft"], 2)
        sheets = bounded(item["sheet_ids"], MAX_ROWS)
        if not sheets:
            raise ValueError("Choose at least one destination sheet per view.")
        for sheet_id in sheets:
            pair = (element_id(sheet_id), view_id)
            if pair in seen or len(rows) >= MAX_ROWS:
                raise ValueError("Review at most 50 placements with no duplicate sheet and view pair.")
            seen.add(pair)
            rows.append((pair, point))
    if not rows or not isinstance(request["sheets"], dict) or set(request["sheets"]) != {pair[0] for pair, _ in rows}:
        raise ValueError("Supply measured usable bounds for every reviewed sheet.")
    for scope in request["sheets"].values():
        fields(scope, ("usable_ft", "title_block_id", "title_block_uid", "revision_sha256"))
        rectangle(scope["usable_ft"])
        element_id(scope["title_block_id"])
        uid(scope["title_block_uid"])
        token(scope["revision_sha256"], SHA256)
    return rows


def indexed(rows, limit):
    result = {}
    for row in bounded(rows, limit):
        identifier = element_id(row["id"])
        uid(row["uid"])
        if identifier in result:
            raise ValueError("The inventory repeats an element identity.")
        result[identifier] = row
    return result


def placement_geometry(placement):
    if placement["kind"] not in KINDS:
        raise ValueError("The placement kind is unavailable.")
    coordinates(placement["point_ft"], 2)
    shapes = bounded(placement["rectangles_ft"], 1 if placement["kind"] == "schedule" else 2)
    if not shapes:
        raise ValueError("The placement body has no verified bounds.")
    return [rectangle(item) for item in shapes]


def sheet_contents(sheet):
    geometry = {}
    pairs = defaultdict(list)
    placements = indexed(sheet["placements"], MAX_GRAPHICS)
    for item in placements.values():
        fields(item, ("id", "uid", "view_id", "kind", "point_ft", "rectangles_ft"))
        geometry[item["id"]] = placement_geometry(item)
        pairs[element_id(item["view_id"])].append(item)
    for item in bounded(sheet["obstacles"], MAX_GRAPHICS):
        fields(item, ("id", "rectangles_ft"))
        identifier = element_id(item["id"])
        shapes = bounded(item["rectangles_ft"], 20)
        if identifier in geometry or not shapes:
            raise ValueError("A sheet obstacle is repeated or lacks verified bounds.")
        geometry[identifier] = [rectangle(shape) for shape in shapes]
    return geometry, pairs


def compute(snapshot):
    fields(snapshot, ("kind", "complete", "document", "request", "views", "sheets", "viewport_eligible_pairs"))
    if snapshot["kind"] != "sheet-placement-snapshot" or snapshot["complete"] is not True:
        raise ValueError("Supply a complete sheet placement snapshot.")
    document = snapshot["document"]
    fields(document, ("project_uid", "path_sha256"))
    uid(document["project_uid"])
    token(document["path_sha256"], SHA256)
    request = snapshot["request"]
    rows = request_rows(request)
    requested_pairs = {pair for pair, _ in rows}
    views = indexed(snapshot["views"], MAX_ROWS)
    sheets = indexed(snapshot["sheets"], MAX_ROWS)
    if set(views) != {pair[1] for pair in requested_pairs} or set(sheets) != set(request["sheets"]):
        raise ValueError("The snapshot does not contain exactly the reviewed sheets and views.")
    eligible_pairs = set()
    for pair in bounded(snapshot["viewport_eligible_pairs"], MAX_ROWS):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("An eligibility result has an invalid sheet and view pair.")
        pair = tuple(element_id(item) for item in pair)
        if pair not in requested_pairs or pair in eligible_pairs:
            raise ValueError("The placement eligibility scope changed.")
        eligible_pairs.add(pair)

    graphics = set()
    existing = {}
    for sheet_id, sheet in sheets.items():
        fields(sheet, ("id", "uid", "number", "outline_ft", "title_block_id", "title_block_uid",
                       "revision_sha256", "ownership_ok", "placements", "obstacles"))
        text(sheet["number"])
        scope = request["sheets"][sheet_id]
        if any(sheet[key] != scope[key] for key in ("title_block_id", "title_block_uid", "revision_sha256")):
            raise ValueError("The measured title block or revision state has changed.")
        if not fits(scope["usable_ft"], rectangle(sheet["outline_ft"]), 0):
            raise ValueError("A reviewed usable rectangle falls outside the actual sheet.")
        if type(sheet["ownership_ok"]) is not bool:
            raise ValueError("The destination sheet ownership state is unavailable.")
        geometry, existing[sheet_id] = sheet_contents(sheet)
        if graphics.intersection(geometry):
            raise ValueError("A graphic appears on more than one reviewed sheet.")
        graphics.update(geometry)
        if len(graphics) > MAX_GRAPHICS:
            raise ValueError("The combined sheet graphics exceed the review limit.")
    for view in views.values():
        fields(view, ("id", "uid", "name", "kind", "eligible", "saved_position", "placed_sheet_ids"))
        text(view["name"])
        if (view["kind"] not in KINDS or type(view["eligible"]) is not bool
                or type(view["saved_position"]) is not bool):
            raise ValueError("The source view eligibility evidence is incomplete.")
        placements = [element_id(item) for item in bounded(view["placed_sheet_ids"], MAX_GRAPHICS)]
        if len(placements) != len(set(placements)):
            raise ValueError("The source placement inventory repeats a sheet.")
        local = {sheet_id for sheet_id, pairs in existing.items() if view["id"] in pairs}
        if local != set(placements).intersection(sheets):
            raise ValueError("The source and destination inventories disagree about an existing placement.")

    ordinary_count = Counter(pair[1] for pair, _ in rows if views[pair[1]]["kind"] == "view")
    before, after, warnings, steps = [], [], [], []
    for index, ((sheet_id, view_id), point) in enumerate(rows, 1):
        sheet, view = sheets[sheet_id], views[view_id]
        anchor = "Schedule insertion point" if view["kind"] == "schedule" else "Viewport box center"
        matching = existing[sheet_id].get(view_id, [])
        old = matching[0] if len(matching) == 1 else None
        row = [str(index), sheet["number"], view["name"], anchor]
        current = row + [old["point_ft"] if old else None, old["id"] if old else None, ""]
        reason = None
        action = "Place"
        if matching:
            if old and old["kind"] == view["kind"] and same_point(old["point_ft"], point):
                action = "Already placed"
            else:
                reason = "This view has an existing placement at a different anchor or multiple placements."
        elif not view["eligible"] or view["saved_position"]:
            reason = "The source is ineligible or uses an assigned Saved Position."
        elif view["kind"] == "view" and (ordinary_count[view_id] > 1 or view["placed_sheet_ids"]):
            reason = "An ordinary view can only be placed on one sheet."
        elif not sheet["ownership_ok"]:
            reason = "Destination sheet ownership or central update status blocks placement."
        elif view["kind"] != "schedule" and (sheet_id, view_id) not in eligible_pairs:
            reason = "Revit did not confirm viewport placement on this sheet."
        elif not fits(point + point, request["sheets"][sheet_id]["usable_ft"], 0):
            reason = "The requested anchor is outside the measured usable rectangle."

        if reason:
            action = "Blocked"
            warnings.append({"row": str(index), "text": reason, "blocking": True})
        elif action == "Place":
            steps.append({"sheet_id": sheet_id, "sheet_uid": sheet["uid"], "view_id": view_id,
                          "view_uid": view["uid"], "kind": view["kind"], "point_ft": point})
        before.append(current)
        after.append(current[:-1] + [action] if matching else row + [point, None, action])

    columns = ["Row", "Sheet number", "View", "Anchor", "Position ft", "Existing ID", "Action"]
    plan = {"kind": "change-preview", "recipe": "sheet-placement", "title": "Place existing views on sheets",
            "columns": columns, "keyColumn": columns[0], "changedColumns": ["Position ft", "Action"],
            "before": before, "after": after, "warnings": warnings, "blocked": bool(warnings),
            "steps": [] if warnings else steps, "document": document, "request": request,
            "ownership_ids": [] if warnings else sorted({step["sheet_id"] for step in steps}, key=int),
            "basis_sha256": hashlib.sha256(encode(snapshot).encode("ascii")).hexdigest()}
    encode(plan)
    return plan


def verify_layout(plan, snapshot, placements):
    """Check complete measured geometry from one regenerated native batch."""
    if compute(snapshot) != plan or plan["blocked"] or len(placements) != len(plan["steps"]):
        raise ValueError("The measured placements do not belong to the approved sheet plan.")
    sheets = {item["id"]: item for item in snapshot["sheets"]}
    occupied = {key: sheet_contents(sheet)[0] for key, sheet in sheets.items()}
    seen = {identifier for geometry in occupied.values() for identifier in geometry}
    for step, placed in zip(plan["steps"], placements):
        fields(placed, ("id", "uid", "sheet_id", "sheet_uid", "view_id", "view_uid", "kind",
                        "point_ft", "rectangles_ft"))
        identifier = element_id(placed["id"])
        uid(placed["uid"])
        shapes = placement_geometry(placed)
        if (identifier in seen or any(placed[field] != step[field] for field in
                                      ("sheet_id", "sheet_uid", "view_id", "view_uid", "kind"))
                or not same_point(placed["point_ft"], step["point_ft"])):
            raise ValueError("A placement identity or its measured anchor differs from the approved plan.")
        sheet_id = step["sheet_id"]
        clearance = plan["request"]["clearance_ft"]
        if any(not fits(shape, plan["request"]["sheets"][sheet_id]["usable_ft"], clearance) for shape in shapes):
            raise ValueError("A measured placement or viewport label exceeds the reviewed sheet area.")
        if any(overlap(shape, obstacle, clearance) for shape in shapes
               for other in occupied[sheet_id].values() for obstacle in other):
            raise ValueError("A measured placement or viewport label overlaps another sheet object.")
        seen.add(identifier)
        occupied[sheet_id][identifier] = shapes
    return placements


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)
    try:
        snapshot = load(args.snapshot, envelope=True)
        if isinstance(snapshot, dict) and "result" in snapshot:
            snapshot = read_payload(snapshot, "snapshot_json")
        print(encode(compute(snapshot)))
    except (ValueError, TypeError, KeyError, AttributeError, OSError, RecursionError) as error:
        print(f"Cannot plan sheet placement: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
