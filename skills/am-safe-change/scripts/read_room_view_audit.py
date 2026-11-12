"""Read independent baseline or post-trial evidence for the room-view fixture."""

from hashlib import sha256
import json
import re
from uuid import uuid4

from pyrevit.api import DB

import read_room_finish_boundaries as reader


TRANSACTION = re.compile(r"\AArchi Magic room view trial [0-9a-f]{32}\Z")
TOKEN = re.compile(r"\A[0-9a-f]{32}\Z")
MAX_ADDITIONS = 160
MAX_CONTROL_PARAMETERS = 512


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def control_evidence(document, control):
    element = reader.native_element(document, control["id"])
    if element is None or element.UniqueId != control["uid"]:
        raise ValueError("The independent control no longer resolves by ID and UID.")
    identity = reader.element_identity(element, True)
    requested = control["parameter_id"]
    if not hasattr(requested, "strip") or not requested.lstrip("-").isdigit():
        raise ValueError("Choose one native control parameter by its signed ID.")
    matches = []
    for index, parameter in enumerate(element.Parameters):
        if index >= MAX_CONTROL_PARAMETERS:
            raise ValueError("The unrelated control exceeds the parameter read limit.")
        if str(parameter.Id.Value) == requested:
            matches.append(parameter)
    if len(matches) != 1 or not matches[0].HasValue:
        raise ValueError("The unrelated control parameter has no unambiguous native value.")
    parameter = matches[0]
    storage = parameter.StorageType
    if storage == DB.StorageType.Double:
        value = parameter.AsDouble()
    elif storage == DB.StorageType.Integer:
        value = parameter.AsInteger()
    elif storage == DB.StorageType.String:
        value = parameter.AsString()
    elif storage == DB.StorageType.ElementId:
        value = str(parameter.AsElementId().Value)
    else:
        raise ValueError("The unrelated control uses an unsupported parameter type.")
    return {"element": identity, "parameter_id": requested, "storage": str(storage), "value": value}


def inventory(document, control):
    from System.Diagnostics import Stopwatch

    clock = Stopwatch.StartNew()
    views = DB.FilteredElementCollector(document)
    rows = []
    try:
        views.OfClass(DB.View)
        for index, view in enumerate(views):
            if index >= reader.MAX_VIEW_NAMES or clock.ElapsedMilliseconds >= reader.MAX_READ_MILLISECONDS:
                raise ValueError("The exact existing view inventory exceeds the read limit.")
            row = reader.element_identity(view, True)
            row.update(name=view.Name, view_type=str(view.ViewType), is_template=view.IsTemplate,
                       type_id=str(view.GetTypeId().Value), template_id=str(view.ViewTemplateId.Value))
            rows.append(row)
    finally:
        views.Dispose()
    result = encode({"views": sorted(rows, key=lambda row: int(row["id"])),
                     "control": control_evidence(document, control)})
    if len(result) > reader.MAX_BYTES or clock.ElapsedMilliseconds >= reader.MAX_READ_MILLISECONDS:
        raise ValueError("The existing model audit exceeds the evidence limit.")
    return result


def absence(document, additions):
    if not isinstance(additions, list) or len(additions) > MAX_ADDITIONS:
        raise ValueError("Use the complete bounded list of trial additions.")
    ids, uids = set(), set()
    for item in additions:
        if not isinstance(item, dict) or set(item) != {"id", "uid"}:
            raise ValueError("Every trial addition needs an exact ID and UID.")
        identifier, uid = item["id"], item["uid"]
        if (not hasattr(identifier, "strip") or not hasattr(uid, "strip") or not uid.strip()
                or identifier in ids or uid in uids):
            raise ValueError("Trial addition identities must be distinct.")
        if reader.native_element(document, identifier) is not None or document.GetElement(uid) is not None:
            raise ValueError("A trial addition still resolves in the fresh model.")
        ids.add(identifier)
        uids.add(uid)
    return sorted(additions, key=lambda item: int(item["id"]))


def audit(document, args):
    if (document is None or document.IsFamilyDocument or document.IsModifiable or document.IsWorkshared):
        raise ValueError("Read the saved non-workshared trial fixture outside a transaction.")
    name = args.get("trial_transaction")
    additions = args.get("additions", [])
    if (name is None) != (additions == []) or (
            name is not None and (not hasattr(name, "strip") or TRANSACTION.match(name) is None)):
        raise ValueError("Use the exact trial name and its additions for the post-rollback audit.")
    snapshot, _ = reader.read_room_view_snapshot(document, args["room_views"])
    measured = inventory(document, args["unrelated_control"])
    snapshot_text = encode(snapshot)
    report = {"status": "ok", "audit_token": uuid4().hex, "trial_transaction": name,
              "snapshot_json": snapshot_text, "inventory_json": measured,
              "confirmed_absent": absence(document, additions)}
    if len(encode(report)) > reader.MAX_BYTES:
        raise ValueError("The independent room view audit exceeds the evidence limit.")
    return report


if "doc" in globals() and "inputs" in globals():
    try:
        result = audit(doc, inputs)
    except Exception as error:
        result = {"status": "blocked", "reason": str(error) if isinstance(error, ValueError) else type(error).__name__}
