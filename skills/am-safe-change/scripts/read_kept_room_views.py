"""Read a kept room-view batch after the Revit decision. Use run_query only."""

import json
from uuid import uuid4

from pyrevit.api import DB

import probe_room_views as native
import read_room_finish_boundaries as reader
import read_room_view_audit as audit


def exact_element(document, identity, kind):
    if not isinstance(identity, dict) or set(identity) != {"id", "uid"}:
        raise ValueError("Every kept result needs an exact ID and UID.")
    element = native.resolve(document, identity)
    by_uid = document.GetElement(identity["uid"])
    if not isinstance(element, kind) or by_uid is None or by_uid.Id.Value != element.Id.Value:
        raise ValueError("A kept result does not resolve to the expected native view or marker.")
    return element


def reviewed_input(args):
    evidence = args["room_view_preflight"]
    snapshot_json, proposal_json = evidence["snapshot_json"], evidence["proposal_json"]
    for value in (snapshot_json, proposal_json):
        if (not hasattr(value, "encode") or len(value) > reader.MAX_BYTES
                or len(value.encode("utf-8")) > reader.MAX_BYTES):
            raise ValueError("The reviewed room view evidence exceeds the read limit.")
    proposal_sha = audit.digest(proposal_json)
    proposal = json.loads(proposal_json)
    if (proposal_sha != evidence["proposal_sha256"] or proposal_sha != args["approved_proposal_sha256"]
            or audit.digest(snapshot_json) != proposal["snapshot_sha256"]):
        raise ValueError("The approved room view evidence does not match its digests.")
    return proposal_sha, json.loads(snapshot_json), proposal["views"]


def persisted_inventory(before, after, new_views):
    if before["control"] != after["control"]:
        raise ValueError("The unrelated fixture control changed after Keep.")
    current = {}
    for row in after["views"]:
        if row["id"] in current:
            raise ValueError("The persisted view inventory contains duplicate IDs.")
        current[row["id"]] = row
    for identity in new_views:
        row = current.pop(identity["id"], None)
        if row is None or row["uid"] != identity["uid"]:
            raise ValueError("A kept view is missing from the persisted inventory.")
    if sorted(current.values(), key=lambda row: int(row["id"])) != before["views"]:
        raise ValueError("Existing native views changed after Keep.")


def read_kept(document, args):
    if document is None or document.IsFamilyDocument or document.IsModifiable or document.IsWorkshared:
        raise ValueError("Read the saved non-workshared project outside a transaction.")
    proposal_sha, before, proposed = reviewed_input(args)
    additions = args["kept_views"]
    if (not isinstance(proposed, list) or not 1 <= len(proposed) <= reader.MAX_VIEWS
            or not isinstance(additions, list) or len(additions) != len(proposed)):
        raise ValueError("Read every kept room view from the reviewed proposal.")
    settings = args["fixture_settings"]
    scale, clip_mode = settings["scale"], settings["far_clip_mode"]
    if type(scale) is not int or not 1 <= scale <= 24000 or type(clip_mode) is not int or clip_mode not in (1, 2):
        raise ValueError("The reviewed view scale and finite far clip mode are required.")
    current, _ = reader.read_room_view_snapshot(document, args["room_views"])
    expected = dict(before, view_names=sorted(before["view_names"] + [view["name"] for view in proposed]))
    if current != expected:
        raise ValueError("The saved room scope or expected view names changed after Keep.")

    baseline_inventory = args["baseline_inventory_json"]
    if (not hasattr(baseline_inventory, "encode") or len(baseline_inventory) > reader.MAX_BYTES
            or len(baseline_inventory.encode("utf-8")) > reader.MAX_BYTES
            or audit.digest(baseline_inventory) != args["baseline_inventory_sha256"]):
        raise ValueError("The independent baseline view inventory is missing or changed.")
    inventory = audit.inventory(document, args["unrelated_control"])
    rooms = {item["id"]: item for item in current["rooms"]}
    view_identities, measurements = [], []
    ids, uids = set(), set()
    for spec, item in zip(proposed, additions):
        if item["name"] != spec["name"]:
            raise ValueError("The kept views are not in the reviewed proposal order.")
        if spec["kind"] == "elevation":
            if set(item) != {"name", "view", "marker", "slot"} or type(item["slot"]) is not int or item["slot"] != 0:
                raise ValueError("The kept elevation needs its exact reviewed marker slot.")
            marker = exact_element(document, item["marker"], DB.ElevationMarker)
        elif spec["kind"] == "section" and set(item) == {"name", "view"}:
            marker = None
        else:
            raise ValueError("The kept section has unexpected marker evidence.")
        view = exact_element(document, item["view"], DB.ViewSection)
        for element in (view, marker) if marker is not None else (view,):
            identifier, uid = str(element.Id.Value), element.UniqueId
            if identifier in ids or uid in uids:
                raise ValueError("The kept views repeat a native view or marker identity.")
            ids.add(identifier)
            uids.add(uid)
        source = rooms[spec["room_id"]]
        room = native.resolve(document, source)
        phase = native.resolve(document, source["phase"])
        template = native.resolve(document, spec["template"])
        measurements.append(native.verify_view(view, marker, item.get("slot"), spec,
                                               room, template, scale, phase, clip_mode))
        view_identities.append(item["view"])
    persisted_inventory(json.loads(baseline_inventory), json.loads(inventory), view_identities)
    receipt = {"status": "kept_room_views_read", "audit_token": uuid4().hex,
               "proposal_sha256": proposal_sha, "fixture_settings": settings,
               "baseline_inventory_sha256": args["baseline_inventory_sha256"],
               "persisted_inventory_sha256": audit.digest(inventory), "views": measurements}
    payload = audit.encode(receipt)
    if len(payload) > reader.MAX_BYTES:
        raise ValueError("The kept view receipt exceeds the read limit.")
    return {"status": receipt["status"], "kept_json": payload}


if "doc" in globals() and "inputs" in globals():
    try:
        result = read_kept(doc, inputs)
    except Exception as error:
        result = {"status": "blocked", "reason": str(error) if isinstance(error, ValueError) else type(error).__name__}
