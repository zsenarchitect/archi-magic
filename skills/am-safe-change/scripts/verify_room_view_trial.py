"""Verify a room-view trial using the three saved pyRevit bridge responses."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import room_views_preview as planner


TRANSACTION = re.compile(r"Archi Magic room view trial [0-9a-f]{32}")
TOKEN = re.compile(r"[0-9a-f]{32}")
EFFECTS = ("added", "modified", "deleted")
TOLERANCE = 1e-5


def decode(value):
    if not isinstance(value, str) or len(value.encode("utf-8")) > planner.MAX_BYTES:
        raise ValueError("The saved result does not contain complete room view evidence.")
    return json.loads(value)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def run_record(raw, mode, transactions):
    record = raw.get("response", raw)
    decision = "rolled_back" if mode == "query" else "no_changes"
    if (record.get("mode") != mode or record.get("status") != "ok"
            or record.get("decision") != decision
            or type(record.get("exit_code")) is not int or record["exit_code"] != 0
            or record.get("result_truncated") is not False or record.get("approval") is not None
            or record.get("error") is not None or not planner.text(record.get("run_id"))
            or any(record.get(key) for key in ("failures", "dialogs", "warnings", "blocked", "unclosed_documents"))
            or not isinstance(record.get("result"), dict)):
        raise ValueError("Use a complete successful %s bridge response with no net changes." % mode)
    changes = record["changes"]
    if (changes.get("other_documents") or changes.get("by_category") != {}
            or changes.get("transactions") != transactions):
        raise ValueError("The bridge effect record does not match the room view run.")
    for key in EFFECTS:
        if (type(changes.get(key + "_count")) is not int or changes[key + "_count"] != 0
                or changes.get(key) != [] or changes.get(key + "_ids")):
            raise ValueError("The bridge did not prove a complete empty net effect set.")
    return record


def exact_additions(rows):
    additions = {}
    uids = set()
    for raw in planner.bounded_list(rows, 160, "Trial additions", 1):
        item = planner.identity(raw)
        if set(raw) != {"id", "uid"} or item["id"] in additions or item["uid"] in uids:
            raise ValueError("The trial additions do not have distinct exact identities.")
        additions[item["id"]] = item
        uids.add(item["uid"])
    return [additions[key] for key in sorted(additions, key=int)]


def close(actual, expected):
    return all(abs(a - b) <= TOLERANCE for a, b in zip(actual, expected))


def measured_additions(receipt, preview, snapshot):
    proposal = preview["proposal"]["views"]
    measured = planner.bounded_list(receipt["views"], len(proposal), "Measured views", len(proposal))
    settings = receipt["fixture_settings"]
    if (type(settings.get("scale")) is not int or not 1 <= settings["scale"] <= 24000
            or type(settings.get("far_clip_mode")) is not int or settings["far_clip_mode"] not in (1, 2)):
        raise ValueError("The measured fixture view settings are invalid.")
    rooms = {room["id"]: room for room in snapshot["rooms"]}
    identities = []
    for spec, row in zip(proposal, measured):
        origin = planner.geometry.point(row["view_origin"], 3)
        direction = planner.geometry.point(row["direction"], 3)
        depth, = planner.geometry.point([row["far_extent"]], 1)
        if (any(spec[key] != row.get(key) for key in ("name", "kind", "room_id", "template"))
                or row.get("type_id") != spec["type"]["id"]
                or row.get("phase") != rooms[spec["room_id"]]["phase"]
                or any(row.get(key) != settings[key] for key in ("scale", "far_clip_mode"))
                or abs(depth - spec["far_extent"]) > TOLERANCE
                or not close(direction, [-axis for axis in spec["look"]])
                or abs(sum((a - b) * axis for a, b, axis in zip(origin, spec["origin"], spec["look"]))) > TOLERANCE):
            raise ValueError("A measured room view differs from the independently recomputed preview.")
        points = [planner.geometry.point(item, 2) for item in
                  planner.bounded_list(row["crop"], 4, "Crop corners", 4)]
        low, high = [tuple(bound(axis) for axis in zip(*points)) for bound in (min, max)]
        if (set(points) != {(x, y) for x in (low[0], high[0]) for y in (low[1], high[1])}
                or not close([b - a for a, b in zip(low, high)],
                             [b - a for a, b in zip(spec["crop_min"], spec["crop_max"])])):
            raise ValueError("A measured crop differs from the reviewed rectangle.")
        targets = [(x, y) for x in (spec["crop_min"][0], spec["crop_max"][0])
                   for y in (spec["crop_min"][1], spec["crop_max"][1])]
        depths = []
        for corner in planner.bounded_list(row["crop_world"], 4, "World crop corners", 4):
            delta = [a - b for a, b in zip(planner.geometry.point(corner, 3), spec["origin"])]
            projected = [sum(axis * component for axis, component in zip(spec["right"], delta)), delta[2]]
            depths.append(sum(axis * component for axis, component in zip(spec["look"], delta)))
            matches = [target for target in targets if close(projected, target)]
            if len(matches) != 1:
                raise ValueError("A measured crop is displaced from its reviewed room surface.")
            targets.remove(matches[0])
        if max(depths) - min(depths) > TOLERANCE:
            raise ValueError("The measured crop corners do not lie in one view plane.")
        identities.append(row["view"])
        if spec["kind"] == "elevation":
            marker = planner.geometry.point(row["marker_point"], 3)
            if row.get("slot") != 0 or not close(marker[:2], spec["origin"][:2]):
                raise ValueError("A measured elevation marker differs from the reviewed position.")
            identities.append(row["marker"])
        elif any(key in row for key in ("marker", "slot", "marker_point")):
            raise ValueError("A section unexpectedly has an elevation marker.")
    return exact_additions(identities)


def verify(before_raw, trial_raw, after_raw, approved_digest):
    before = run_record(before_raw, "query", [])
    after = run_record(after_raw, "query", [])
    raw_trial = trial_raw.get("response", trial_raw)
    receipt = decode(raw_trial["result"]["trial_json"])
    name = receipt["transaction"]
    if not isinstance(name, str) or not TRANSACTION.fullmatch(name):
        raise ValueError("The receipt does not identify a native room view trial.")
    trial = run_record(trial_raw, "dry_run", [name])
    run_ids = [row["run_id"] for row in (before, trial, after)]
    if len(set(run_ids)) != 3:
        raise ValueError("Use three separate runs for baseline, trial, and post-rollback audit.")
    baseline, fresh = before["result"], after["result"]
    if (trial["result"].get("status") != "trial_rolled_back" or receipt.get("status") != "trial_rolled_back"
            or any(audit.get("status") != "ok" or not TOKEN.fullmatch(audit["audit_token"])
                   for audit in (baseline, fresh))
            or baseline["audit_token"] == fresh["audit_token"] or baseline["trial_transaction"] is not None
            or baseline["confirmed_absent"] != [] or fresh["trial_transaction"] != name
            or any(baseline[key] != fresh[key] for key in ("snapshot_json", "inventory_json"))
            or receipt["baseline_audit"] != {
                "audit_token": baseline["audit_token"],
                "snapshot_sha256": digest(baseline["snapshot_json"]),
                "inventory_sha256": digest(baseline["inventory_json"]),
            }):
        raise ValueError("The three runs do not establish the same reviewed fixture and baseline.")
    snapshot = decode(baseline["snapshot_json"])
    preview = planner.compute(snapshot)
    if preview["blocked"] or approved_digest != preview["proposal_sha256"] or receipt["proposal_sha256"] != approved_digest:
        raise ValueError("The trial did not use the approved recomputed room view proposal.")
    additions = measured_additions(receipt, preview, snapshot)
    if exact_additions(fresh["confirmed_absent"]) != additions:
        raise ValueError("The fresh query did not prove absence of the exact trial additions.")
    ids = [item["id"] for item in additions]
    if receipt["effects"] != [
        {"operation": "TransactionCommitted", "added": ids, "modified": [], "deleted": []},
        {"operation": "TransactionGroupRolledBack", "added": [], "modified": [], "deleted": ids},
    ]:
        raise ValueError("The native effects do not match the measured additions and their rollback.")
    inventory = decode(baseline["inventory_json"])
    existing_views = planner.bounded_list(inventory["views"], planner.MAX_NAME_VISITS, "Existing views")
    existing = {planner.identity(item, versioned=True)["id"] for item in existing_views}
    control = inventory["control"]
    control_id = planner.identity(control["element"], versioned=True)["id"]
    if (not re.fullmatch(r"-?[0-9]+", control["parameter_id"]) or not planner.text(control["storage"])
            or "value" not in control or len(existing) != len(existing_views)):
        raise ValueError("The baseline is missing exact existing view or unrelated control evidence.")
    related = {room["id"] for room in snapshot["rooms"]}
    for room in snapshot["rooms"]:
        related.update(room[key]["id"] for key in ("host_plan", "phase", "level"))
        related.update(source["id"] for source in room["finish_sources"])
        for family in room["families"].values():
            related.update(item["id"] for item in family.values())
    if (sorted(item["name"] for item in existing_views) != sorted(snapshot["view_names"])
            or control_id in related or set(ids) & (existing | related | {control_id})):
        raise ValueError("The exact native effects conflict with the existing model inventory.")
    return {"status": "verified_room_view_trial_rollback", "transaction": name,
            "proposal_sha256": approved_digest, "run_ids": run_ids, "confirmed_absent": additions}


def read(path):
    with path.open("rb") as source:
        raw = source.read(planner.MAX_BRIDGE_BYTES + 1)
    if len(raw) > planner.MAX_BRIDGE_BYTES:
        raise ValueError("The saved bridge response exceeds the read limit.")
    return json.loads(raw.decode("utf-8-sig"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for stage in ("before", "trial", "after"):
        parser.add_argument("--" + stage, required=True, type=Path)
    parser.add_argument("--approved-digest", required=True)
    args = parser.parse_args()
    try:
        result = verify(read(args.before), read(args.trial), read(args.after), args.approved_digest)
        print(json.dumps(result, ensure_ascii=True))
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
