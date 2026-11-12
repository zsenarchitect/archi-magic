"""Run a rollback-only split trial for straight curve-based columns in a disposable Revit fixture."""

from decimal import Decimal
from hashlib import sha256
import json
from uuid import uuid4

from pyrevit.api import DB
from System import Int64

import element_split_preview as planner
import read_element_split_evidence as reader
import verify_element_split as verifier


MAX_EVENTS = 8


def digest(value):
    return sha256(value.encode("utf-8")).hexdigest()


def parse(value):
    return json.loads(value, parse_float=Decimal, object_pairs_hook=planner.exact_object)


def preflight(document, args):
    planner.fields(args, {"mode", "disposable_fixture", "source_request", "approval_json", "approval_sha256"})
    if args["mode"] != "rollback_trial" or args["disposable_fixture"] is not True:
        raise ValueError("Use the rollback trial on an approved disposable fixture.")
    approval_text = args["approval_json"]
    if (not isinstance(approval_text, str) or len(approval_text.encode("utf-8")) > planner.MAX_BYTES
            or digest(approval_text) != args["approval_sha256"]):
        raise ValueError("The reviewed approval digest does not match.")
    source = reader.main(document, args["source_request"])
    snapshot = parse(source["snapshot_json"])
    inventory = parse(source["inventory_json"])
    approved = parse(approval_text)
    plan = planner.preview(snapshot)
    if plan["blocked"] or any(item["category"] not in ("architectural_column", "structural_column")
                              for item in plan["findings"]):
        raise ValueError("This trial requires unblocked architectural or structural columns.")
    expected = {item["id"]: item for item in plan["findings"]}
    baseline = {row["id"]: verifier.parameters(row["parameters"]) for row in inventory["elements"]}
    verifier.reviewed_adjustments(approved, digest(source["snapshot_json"]), digest(source["inventory_json"]),
                                  expected, baseline)
    if any(row["dependencies"] or row["geometry_joins"] for row in inventory["elements"]):
        raise ValueError("A source column has dependencies or geometry joins outside this trial.")
    for row in snapshot["elements"]:
        element = reader.selected(document, {"id": row["id"], "uid": row["uid"]})
        if not isinstance(element.Location, DB.LocationCurve):
            raise ValueError("Point-located columns require a separately qualified normalized split direction.")
    return source, snapshot, inventory, approved, expected


def ordered_axis(element):
    if not isinstance(element, DB.FamilyInstance) or not isinstance(element.Location, DB.LocationCurve):
        raise ValueError("The current result lacks a readable defining column curve.")
    curve = element.Location.Curve
    first, last = reader.line_points(curve)
    tolerance = reader.TOLERANCE_FT
    if abs(first[0] - last[0]) > tolerance or abs(first[1] - last[1]) > tolerance:
        raise ValueError("The current result does not have a vertical defining curve.")
    return curve, first, last


def split_piece(document, pieces, elevation, created):
    tolerance = reader.TOLERANCE_FT
    candidate = None
    for identifier in pieces:
        element = document.GetElement(identifier)
        if element is None:
            raise ValueError("A trial piece no longer resolves.")
        curve, first, last = ordered_axis(element)
        low, high = sorted((first[2], last[2]))
        if low + tolerance < elevation < high - tolerance:
            if candidate is not None:
                raise ValueError("Two pieces contain the same cut elevation.")
            candidate = element, curve, first
    if candidate is None:
        raise ValueError("No piece contains the reviewed cut elevation.")
    element, curve, point = candidate
    projected = curve.Project(DB.XYZ(point[0], point[1], elevation))
    if not element.CanSplit or projected is None or projected.Distance > tolerance:
        raise ValueError("The reviewed cut cannot be projected onto the eligible column.")
    normalized = curve.ComputeNormalizedParameter(projected.Parameter)
    if not 0 < normalized < 1:
        raise ValueError("The native normalized cut is outside the piece.")
    identifier = element.Split(normalized)
    new_id = planner.identifier(str(identifier.Value))
    if identifier in pieces or new_id in created:
        raise ValueError("The split did not return a distinct new piece.")
    created.add(new_id)
    pieces.append(identifier)
    document.Regenerate()


def run(document, args):
    source, snapshot, inventory, approval, expected = preflight(document, args)
    transaction_name = "Archi Magic column split trial " + uuid4().hex
    status = DB.TransactionStatus
    group = DB.TransactionGroup(document, transaction_name)
    transaction = DB.Transaction(document, transaction_name)
    created, observed = set(), set()
    source_ids = set(expected)
    commit_events, rollback_events = [], []
    closing = invalid_event = False

    def record(_sender, event):
        nonlocal invalid_event
        try:
            if not event.GetDocument().Equals(document):
                invalid_event = True
                return
            op = str(event.Operation)
            effects = {}
            for key, items in (("added", event.GetAddedElementIds()), ("modified", event.GetModifiedElementIds()),
                               ("deleted", event.GetDeletedElementIds())):
                effects[key] = set()
                for item in items:
                    if len(effects[key]) >= planner.MAX_PIECES:
                        raise ValueError("The transaction changed too many elements.")
                    effects[key].add(planner.identifier(str(item.Value)))
            observed.update(effects["added"])
            names = list(event.GetTransactionNames())
            if closing and op in ("TransactionRolledBack", "TransactionGroupRolledBack"):
                if (len(rollback_events) >= MAX_EVENTS or (names and names != [transaction_name])
                        or effects["added"] or effects["modified"] - source_ids
                        or effects["deleted"] - created):
                    invalid_event = True
                else:
                    rollback_events.append({"operation": op, "transactions": names, **effects})
            elif not closing and op == "TransactionCommitted" and not commit_events and names == [transaction_name]:
                commit_events.append(effects)
            else:
                invalid_event = True
        except Exception:
            invalid_event = True

    application = document.Application
    failure = readback_text = comparison = None
    rolled_back = regeneration_failed = False
    application.DocumentChanged += record
    try:
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit did not start the reversible column trial.")
        requests = []
        for row in snapshot["elements"]:
            source_id = row["id"]
            ids = [DB.ElementId(Int64(int(source_id)))]
            for cut in sorted(row["cuts"], key=lambda item: item["elevation_ft"]):
                split_piece(document, ids, float(cut["elevation_ft"]), created)
            for identifier in ids:
                element = document.GetElement(identifier)
                requests.append({"source_id": source_id, "result": reader.identity(element)})
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("The provisional column transaction did not finish.")
        if invalid_event or commit_events != [{"added": created, "modified": source_ids, "deleted": set()}]:
            invalid_event = True
            raise ValueError("Revit reported effects outside the reviewed column split.")
        data = reader.main(document, {"host_identity": snapshot["host_identity"], "readback": requests})
        readback_text = data["readback_json"]
        comparison = verifier.verify(snapshot, digest(source["snapshot_json"]), inventory,
                                     digest(source["inventory_json"]), approval, parse(readback_text))
        if comparison["status"] != "matched":
            raise ValueError("Trial geometry, types, or instance parameters differ from the reviewed basis.")
    except Exception as error:
        regeneration_failed = type(error).__name__ == "RegenerationFailedException"
        failure = str(error) if isinstance(error, ValueError) else type(error).__name__
    finally:
        closing = True
        try:
            state = transaction.GetStatus()
            if state == status.Started:
                transaction.RollBack()
                state = transaction.GetStatus()
            if state in (status.Committed, status.RolledBack, status.Uninitialized) and group.GetStatus() == status.Started:
                rolled_back = group.RollBack() == status.RolledBack and group.GetStatus() == status.RolledBack
        except Exception:
            invalid_event = True
        finally:
            application.DocumentChanged -= record

    touched = sorted(created | observed, key=int)
    if regeneration_failed or invalid_event or not rolled_back:
        return {"status": "transaction_unresolved", "reason": failure or "Revit did not establish a clean trial rollback.",
                "observed_additions": touched}
    try:
        if any(document.GetElement(DB.ElementId(Int64(int(value)))) is not None for value in touched):
            raise ValueError("A trial piece still resolves after rollback.")
        if reader.main(document, args["source_request"]) != source:
            raise ValueError("The original column evidence differs after rollback.")
    except Exception as error:
        return {"status": "transaction_unresolved", "reason": str(error), "observed_additions": touched}
    if failure is not None:
        return {"status": "stopped_and_rolled_back", "reason": failure, "observed_additions": touched,
                "comparison": comparison}
    if sum(event["operation"] == "TransactionGroupRolledBack" for event in rollback_events) != 1:
        return {"status": "transaction_unresolved", "reason": "The group rollback event was not verified.",
                "observed_additions": touched}

    def serializable(event):
        return {key: sorted(value, key=int) if isinstance(value, set) else value for key, value in event.items()}

    report = {"status": "trial_rolled_back", "transaction": transaction_name, "approval_sha256": args["approval_sha256"],
              "inner_effects": serializable(commit_events[0]), "rollback_events": list(map(serializable, rollback_events)),
              "comparison": comparison, "readback_json": readback_text}
    encoded = json.dumps(report, ensure_ascii=True, allow_nan=False)
    if len(encoded) > planner.MAX_BYTES:
        return {"status": "stopped_and_rolled_back", "reason": "The native trial receipt exceeds 2 MiB.",
                "observed_additions": touched}
    return {"status": "trial_rolled_back", "trial_json": encoded}


if "inputs" in globals():
    try:
        result = run(doc, inputs)  # noqa: F821
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        result = {"status": "stopped", "reason": str(error)}
