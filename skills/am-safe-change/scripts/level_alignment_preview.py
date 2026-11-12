"""Preview rotation, level-relative offset, or nearest-level changes in Revit feet."""

import argparse
import hashlib
import json
import math
import re
import sys

POSITION_TOLERANCE = 0.001
ROTATION_TOLERANCE = 0.00001
MAX_INPUT = 1024 * 1024
TRANSACTION_NAME = "Trial Revit level alignment"
OPERATIONS = {"rotation", "offset", "nearest_level"}
COLUMNS = ["Element ID", "X ft", "Y ft", "Z ft", "Rotation rad", "Level UID", "Offset ft"]


def uid(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 128


def element_id(value):
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value)
            and int(value) <= 2**63 - 1)


def finite(value):
    return type(value) in (int, float) and -1e9 <= value <= 1e9


def read_state(row):
    point = row.get("point")
    if (row.get("model_point") is not True or not uid(row.get("uid")) or not uid(row.get("type_uid"))
            or not isinstance(point, list) or len(point) != 3 or not all(map(finite, point))
            or not finite(row.get("rotation_rad"))):
        raise ValueError("A verified model point, rotation, instance UID, and type UID are required.")
    level_uid = row.get("level_uid")
    offset = row.get("offset_ft")
    if level_uid is not None and not uid(level_uid):
        raise ValueError("The level UID is invalid.")
    if offset is not None and not finite(offset):
        raise ValueError("The offset is outside the supported range.")
    return {
        "point": point,
        "rotation_rad": row["rotation_rad"],
        "level_uid": level_uid,
        "offset_ft": offset,
    }


def offset_contract(row, state):
    contract = row.get("offset_contract")
    level_z = row.get("level_z_ft")
    offset = state["offset_ft"]
    if (not isinstance(contract, dict)
            or not all(uid(contract.get(key)) for key in ("level_parameter_id", "offset_parameter_id", "length_spec"))
            or contract.get("binding") != "instance"
            or contract.get("level_storage") != "ElementId" or contract.get("offset_storage") != "Double"
            or row.get("level_parameter_uid") != state["level_uid"]
            or not uid(state["level_uid"]) or not finite(level_z) or offset is None
            or abs(state["point"][2] - (level_z + offset)) > POSITION_TOLERANCE):
        raise ValueError("The level and offset contract is missing or disagrees with the insertion point.")
    return contract


def candidate_levels(snapshot):
    rows = snapshot.get("levels")
    if (snapshot.get("levels_complete") is not True or not isinstance(rows, list)
            or not 1 <= len(rows) <= 500):
        raise ValueError("A complete bounded inventory of eligible levels is required.")
    levels = {}
    for row in rows:
        if not isinstance(row, dict) or not uid(row.get("uid")) or not finite(row.get("elevation_ft")):
            raise ValueError("Each eligible level needs its UID and elevation in the verified frame.")
        if row["uid"] in levels:
            raise ValueError("The level inventory has duplicate UIDs.")
        levels[row["uid"]] = row["elevation_ft"]
    return levels


def rotation_delta(current, reference):
    return (reference - current + math.pi) % math.tau - math.pi


def propose(snapshot, row, state, reference, reference_state, levels):
    if row.get("editable") is not True or row.get("supported") is not True:
        raise ValueError("The target is outside the supported editable model scope.")
    operation = snapshot["operation"]
    point = state["point"]
    if operation == "rotation":
        delta = rotation_delta(state["rotation_rad"], reference_state["rotation_rad"])
        if abs(delta) <= ROTATION_TOLERANCE:
            return state
        return dict(state, rotation_rad=(state["rotation_rad"] + delta) % math.tau)

    if snapshot.get("frame_verified") is not True:
        raise ValueError("Verify the common elevation coordinate frame before comparing offsets.")
    contract = offset_contract(row, state)
    if operation == "offset":
        if contract != offset_contract(reference, reference_state):
            raise ValueError("The reference uses a different offset contract.")
        offset = reference_state["offset_ft"]
        if abs(offset - state["offset_ft"]) <= POSITION_TOLERANCE:
            return state
        if row.get("offset_writable") is not True:
            raise ValueError("The target offset parameter is not writable.")
        return dict(state, point=[point[0], point[1], row["level_z_ft"] + offset], offset_ft=offset)

    level_uid = state["level_uid"]
    if level_uid not in levels or abs(levels[level_uid] - row["level_z_ft"]) > POSITION_TOLERANCE:
        raise ValueError("The current level is missing or differs from the verified inventory.")
    ranked = sorted((abs(point[2] - elevation), candidate) for candidate, elevation in levels.items())
    distance, nearest = ranked[0]
    if len(ranked) > 1 and ranked[1][0] - distance <= POSITION_TOLERANCE:
        raise ValueError("Two or more eligible levels are within the nearest-level tie tolerance.")
    if nearest == level_uid:
        return state
    if row.get("level_writable") is not True or row.get("offset_writable") is not True:
        raise ValueError("The level and compensating offset parameters must both be writable.")
    return dict(state, level_uid=nearest, offset_ft=point[2] - levels[nearest])


def display_row(id_value, state):
    return [id_value, *state["point"], state["rotation_rad"], state["level_uid"], state["offset_ft"]]


def calculate(snapshot):
    project = snapshot.get("project")
    targets = snapshot.get("targets")
    operation = snapshot.get("operation")
    if (snapshot.get("kind") != "level-alignment-snapshot" or type(snapshot.get("version")) is not int
            or snapshot["version"] != 1 or operation not in OPERATIONS
            or not isinstance(project, dict) or not uid(project.get("project_uid"))
            or not isinstance(project.get("path_sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", project["path_sha256"])
            or not isinstance(targets, list) or not 1 <= len(targets) <= 30):
        raise ValueError("Provide a saved project, an operation, and up to 30 explicit targets.")

    target_ids = set()
    for row in targets:
        if not isinstance(row, dict) or not element_id(row.get("id")) or row["id"] in target_ids:
            raise ValueError("Each target must have a distinct decimal Revit element ID.")
        target_ids.add(row["id"])

    reference = snapshot.get("reference")
    reference_state = None
    levels = {}
    if operation == "nearest_level":
        if reference is not None:
            raise ValueError("Nearest-level matching does not use a reference element.")
        levels = candidate_levels(snapshot)
    else:
        if not isinstance(reference, dict) or not element_id(reference.get("id")) or reference["id"] in target_ids:
            raise ValueError("Select a read-only reference element outside the target list.")
        reference_state = read_state(reference)

    before, after, warnings, changes = [], [], [], {}
    for row in targets:
        id_value = row["id"]
        try:
            state = read_state(row)
        except ValueError as error:
            warnings.append({"row": id_value, "type": "invalid_target", "text": str(error), "blocking": True})
            before.append([id_value, *([None] * (len(COLUMNS) - 1))])
            after.append(before[-1].copy())
            continue
        before.append(display_row(id_value, state))
        try:
            expected = propose(snapshot, row, state, reference, reference_state, levels)
        except ValueError as error:
            warnings.append({"row": id_value, "type": "blocked_target", "text": str(error), "blocking": True})
            expected = state
        after.append(display_row(id_value, expected))
        if expected != state:
            changes[id_value] = expected

    basis = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    preview = {
        "kind": "change-preview",
        "title": "Match Revit rotation, offset, or reference level",
        "keyColumn": COLUMNS[0],
        "columns": COLUMNS,
        "changedColumns": COLUMNS[1:],
        "before": before,
        "after": after,
        "warnings": warnings,
        "notes": ["Review only. This preview does not contain executable Revit changes."],
        "blocked": bool(warnings),
        "review_only": True,
        "basis_sha256": hashlib.sha256(basis.encode("ascii")).hexdigest(),
        "params": {"operation": operation, "units": "ft"},
        "ownership_ids": [] if warnings else list(changes),
        "steps": [],
    }


    return preview, {} if warnings else changes


def compute(snapshot):
    return calculate(snapshot)[0]


def probe_plan(snapshot):
    preview, changes = calculate(snapshot)
    if preview["blocked"] or not changes:
        raise ValueError("The rollback probe requires a reviewed, unblocked change.")
    request = {"operation": snapshot["operation"], "target_ids": [row["id"] for row in snapshot["targets"]]}
    if "reference" in snapshot:
        request["reference_id"] = snapshot["reference"]["id"]
    if snapshot["operation"] != "rotation":
        if not snapshot.get("frame_evidence"):
            raise ValueError("Read the independently verified frame through the native query first.")
        request["verified_frame"] = snapshot["frame_evidence"]
    return {
        "kind": "level-alignment-rollback-plan",
        "version": 1,
        "request": request,
        "basis_sha256": preview["basis_sha256"],
        "changes": changes,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot")
    parser.add_argument("--probe-plan", action="store_true", help="Output a separate rollback qualification plan")
    args = parser.parse_args()
    try:
        with open(args.snapshot, "rb") as stream:
            raw = stream.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("The saved snapshot exceeds 1 MiB.")
        snapshot = json.loads(raw)
        if not isinstance(snapshot, dict):
            raise ValueError("Expected a level alignment snapshot object.")
        result = probe_plan(snapshot) if args.probe_plan else compute(snapshot)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 1 if result.get("blocked") else 0


if __name__ == "__main__":
    sys.exit(main())
