"""Compare independently measured split effects across runs with different allocated IDs."""

from collections import Counter
from decimal import Decimal
import json
import re

from element_split_preview import MAX_PIECES, bounded_list, exact_object, fields, identifier, number, uid


MAX_EFFECTS = 1002
TRANSACTION = re.compile(r"Archi Magic element split trial [0-9a-f]{32}")
ROLES = {"floor", "sketch", "profile_curve", "sketch_plane"}


def id_set(values):
    ids = [identifier(value) for value in bounded_list(values, 0, MAX_EFFECTS, "effect IDs")]
    if len(ids) != len(set(ids)):
        raise ValueError("The split effects repeat an ID.")
    return set(ids)


def signature(geometry):
    role = geometry["role"]
    if role not in ROLES:
        raise ValueError("The floor effect has an unsupported signature.")
    if role == "profile_curve":
        fields(geometry, {"role", "ends_ft"})
        ends = tuple(sorted(tuple(map(number, bounded_list(row, 3, 3, "curve coordinates")))
                            for row in bounded_list(geometry["ends_ft"], 2, 2, "curve ends")))
        if ends[0] == ends[1]:
            raise ValueError("A created floor profile edge has no length.")
        return role, ends
    if role == "sketch_plane":
        fields(geometry, {"role", "elevation_ft"})
        return role, number(geometry["elevation_ft"])
    fields(geometry, {"role"})
    return (role,)


def stable_effects(receipt):
    comparison = receipt["comparison"]
    if comparison["status"] != "matched" or comparison["differences"]:
        raise ValueError("Only independently matched split pieces can be compared.")
    ids, slots = {}, set()
    sources = set()
    for row in bounded_list(comparison["matches"], 1, MAX_PIECES, "matched pieces"):
        fields(row, {"source_id", "piece", "result_id"})
        source, ordinal, result = identifier(row["source_id"]), row["piece"], identifier(row["result_id"])
        if type(ordinal) is not int or ordinal < 1 or (source, ordinal) in slots or result in ids:
            raise ValueError("The split results lack a unique source-to-piece mapping.")
        slots.add((source, ordinal))
        sources.add(source)
        ids[result] = source, ordinal
    if any(result in sources and slot[0] != result for result, slot in ids.items()):
        raise ValueError("A split result was matched to a different original element.")

    effects = receipt["inner_effects"]
    fields(effects, {"added", "modified", "deleted"})
    added, modified, deleted = (id_set(effects[key]) for key in ("added", "modified", "deleted"))
    if added & modified or added & deleted or modified & deleted:
        raise ValueError("The provisional split has overlapping effect lists.")
    retained = {(source, ids[source][1]) for source in sources if source in ids}
    if "floor_effects" not in receipt:
        if deleted or modified != sources or added != ids.keys() - sources or sources - ids.keys():
            raise ValueError("The wall or column effects do not match the measured piece identities.")
        native_effects = Counter(("piece", ids[value]) for value in added)
    else:
        if len(sources) != 1 or not sources <= deleted or not ids.keys() <= added or modified or retained:
            raise ValueError("The floor effects do not match the measured replacements.")
        native_effects, seen, required = Counter(), set(), Counter()
        for row in bounded_list(receipt["floor_effects"], 1, MAX_EFFECTS, "owned floor effects"):
            fields(row, {"id", "geometry", "piece_ids"})
            element = identifier(row["id"])
            owners = id_set(row["piece_ids"])
            if element not in added or element in seen or not owners or owners - ids.keys():
                raise ValueError("The added floor effects do not match their measured owners.")
            seen.add(element)
            geometry = signature(row["geometry"])
            role = geometry[0]
            if role == "floor" and (element not in ids or owners != {element}):
                raise ValueError("A created floor effect lacks its matching result identity.")
            if role != "floor" and element in ids:
                raise ValueError("A result floor is incorrectly identified as a sketch member.")
            if role in ("floor", "sketch"):
                if len(owners) != 1:
                    raise ValueError("A floor or sketch has ambiguous ownership.")
                required[(role, ids[next(iter(owners))])] += 1
            native_effects[(geometry, tuple(sorted(ids[owner] for owner in owners)))] += 1
        if seen != added or required != Counter((role, slot) for role in ("floor", "sketch") for slot in slots):
            raise ValueError("The added floor effects do not cover every floor and sketch.")
    return ids, (modified, deleted, retained, native_effects)


def measurements(receipt, ids):
    raw = json.loads(receipt["readback_json"], parse_float=Decimal, object_pairs_hook=exact_object)
    result, uids = {}, set()
    for row in raw["pieces"]:
        element, element_uid = identifier(row["id"]), uid(row["uid"])
        if (element not in ids or row["source_id"] != ids[element][0] or ids[element] in result or element_uid in uids):
            raise ValueError("The readback and piece mapping disagree.")
        uids.add(element_uid)
        stable = {key: value for key, value in row.items() if key not in ("id", "uid")}
        if element == row["source_id"]:
            stable["retained_uid"] = element_uid
        result[ids[element]] = stable
    if len(result) != len(ids):
        raise ValueError("The readback omits a matched piece.")
    native = {}
    for key in ("wall_measurements", "floor_measurements"):
        native[key] = {ids[element]: row for element, row in receipt.get(key, {}).items()}
    return result, native, receipt["column_cuts"]


def match_replay(trial, replay):
    if trial.get("status") != "trial_rolled_back" or replay.get("status") not in (
            "trial_rolled_back", "awaiting_bridge_decision"):
        raise ValueError("Use a rolled-back trial and a measured replay.")
    required = ("snapshot_sha256", "inventory_sha256", "approval_sha256", "controls_sha256")
    for key in (*required, "floor_cascade_sha256"):
        value = trial.get(key)
        needs_digest = key in required or value is not None
        valid_digest = isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
        if value != replay.get(key) or (needs_digest and not valid_digest):
            raise ValueError("The replay does not use the same reviewed split evidence.")
    if trial["transaction"] == replay["transaction"] or not all(
            TRANSACTION.fullmatch(item["transaction"]) for item in (trial, replay)):
        raise ValueError("Use an independently measured replay transaction.")
    prior_ids, prior_effects = stable_effects(trial)
    new_ids, new_effects = stable_effects(replay)
    if prior_effects != new_effects:
        raise ValueError("The replay has different source or created-element effects.")
    if measurements(trial, prior_ids) != measurements(replay, new_ids):
        raise ValueError("The replay piece geometry, parameters, or native measurements differ.")
    return {"status": "split_replay_matched", "transaction": replay["transaction"],
            "result_ids": sorted(new_ids, key=int)}
