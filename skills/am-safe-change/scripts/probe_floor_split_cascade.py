"""Measure a floor's native deletion cascade in a rollback-only disposable Revit trial."""

from decimal import Decimal
from hashlib import sha256
import json

from pyrevit.api import DB
from System import Int64

import element_split_preview as planner
import read_element_split_evidence as reader


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def eid(value):
    return DB.ElementId(Int64(int(value)))


def floor_members(document, source):
    floor = reader.selected(document, source)
    if not isinstance(floor, DB.Floor):
        raise ValueError("Select exactly one reviewed floor for cascade discovery.")
    sketch = document.GetElement(floor.SketchId)
    if not isinstance(sketch, DB.Sketch) or sketch.OwnerId.Value != floor.Id.Value:
        raise ValueError("The floor sketch ownership could not be established.")
    budget = reader.ReadBudget()
    allowed = {row["id"]: row for row in reader.dependency_rows(document, sketch.GetAllElements(), budget)}
    floor_row, sketch_row = reader.identity(floor), reader.identity(sketch)
    allowed.update({row["id"]: row for row in (floor_row, sketch_row)})
    for element in (floor, sketch):
        dependencies = reader.dependency_rows(document, element.GetDependentElements(None), budget)
        if any(allowed.get(row["id"]) != row for row in dependencies):
            raise ValueError("The floor or sketch has a dependent outside its reviewed owned elements.")
    return floor_row, sketch_row, allowed


def preflight(document, args):
    planner.fields(args, {"mode", "disposable_fixture", "source_request", "snapshot_sha256", "inventory_sha256"})
    if args["mode"] != "floor_cascade_trial" or args["disposable_fixture"] is not True:
        raise ValueError("Use an approved disposable floor cascade trial.")
    request = args["source_request"]
    planner.fields(request, {"host_identity", "scope"})
    scope = planner.bounded_list(request["scope"], 1, 1, "source floor")[0]
    source = reader.main(document, request)
    if (digest(source["snapshot_json"]) != args["snapshot_sha256"]
            or digest(source["inventory_json"]) != args["inventory_sha256"]):
        raise ValueError("The floor source differs from the reviewed snapshot or inventory.")
    snapshot = json.loads(source["snapshot_json"], parse_float=Decimal, object_pairs_hook=planner.exact_object)
    if snapshot["elements"][0]["category"] != "floor" or planner.preview(snapshot)["blocked"]:
        raise ValueError("Use an unblocked split preview for the selected floor.")
    floor, sketch, allowed = floor_members(document, scope["source"])
    inventory = json.loads(source["inventory_json"])
    if inventory["elements"][0]["geometry_joins"]:
        raise ValueError("A joined floor needs separate native qualification.")
    return source, floor, sketch, allowed


def run(document, args):
    before, floor, sketch, allowed = preflight(document, args)
    status = DB.TransactionStatus
    transaction = DB.Transaction(document, "Archi Magic floor cascade discovery")
    observed = set()
    failure = None
    restored = regeneration_failed = delete_read_complete = False
    try:
        if transaction.Start() != status.Started:
            raise ValueError("Revit did not start the reversible floor discovery.")
        for item in document.Delete(eid(floor["id"])):
            if len(observed) >= reader.MAX_DEPENDENCIES + 2:
                raise ValueError("The floor deletion exceeds the evidence budget.")
            value = planner.identifier(str(item.Value))
            if value in observed:
                raise ValueError("Revit repeated an ID in the floor deletion set.")
            observed.add(value)
        delete_read_complete = True
        if {floor["id"], sketch["id"]} - observed or observed - allowed.keys():
            raise ValueError("The native deletion set differs from the permitted floor and sketch ownership.")
        if any(document.GetElement(eid(value)) is not None for value in observed):
            raise ValueError("A deleted floor member still resolves inside the discovery transaction.")
    except Exception as error:
        regeneration_failed = type(error).__name__ == "RegenerationFailedException"
        failure = str(error) if isinstance(error, ValueError) else type(error).__name__
    finally:
        try:
            if transaction.GetStatus() == status.Started:
                restored = (transaction.RollBack() == status.RolledBack
                            and transaction.GetStatus() == status.RolledBack)
        except Exception:
            restored = False

    ids = sorted(observed, key=int)
    if regeneration_failed or not restored or not delete_read_complete or observed - allowed.keys():
        return {"status": "transaction_unresolved", "reason": failure or "The floor rollback was not established.",
                "observed_deletions": ids}
    try:
        after = reader.main(document, args["source_request"])
        if after != before or floor_members(document, floor) != (floor, sketch, allowed):
            raise ValueError("The floor or its owned sketch members differ after rollback.")
    except Exception as error:
        return {"status": "transaction_unresolved", "reason": str(error), "observed_deletions": ids}
    if failure is not None:
        return {"status": "stopped_and_rolled_back", "reason": failure, "observed_deletions": ids}

    receipt = {"kind": "element-split-floor-cascade", "host_identity": args["source_request"]["host_identity"],
               "source": floor, "sketch": sketch, "snapshot_sha256": args["snapshot_sha256"],
               "inventory_sha256": args["inventory_sha256"], "deleted": [allowed[value] for value in ids]}
    encoded = json.dumps(receipt, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded) > planner.MAX_BYTES:
        return {"status": "stopped_and_rolled_back", "reason": "The floor cascade receipt exceeds 2 MiB."}
    return {"status": "floor_cascade_rolled_back", "cascade_json": encoded}


if "inputs" in globals():
    try:
        result = run(doc, inputs)  # noqa: F821
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        result = {"status": "stopped", "reason": str(error)}
