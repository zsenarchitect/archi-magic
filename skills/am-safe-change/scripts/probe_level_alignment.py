"""Qualify a reviewed level-alignment change with mandatory rollback in Revit 2026."""

from Autodesk.Revit.Exceptions import RegenerationFailedException
from pyrevit.api import DB

from level_alignment_preview import POSITION_TOLERANCE, ROTATION_TOLERANCE, probe_plan, read_state, rotation_delta
from read_level_alignment import get, key, read_snapshot

TRANSACTION_NAME = "Trial Revit level alignment"
PHYSICAL_FIELDS = {"point", "rotation_rad", "level_id", "level_uid", "level_parameter_uid", "level_z_ft", "offset_ft"}


def update(document, row, expected, operation, levels, reference):
    element = get(document, row["id"])
    if operation == "rotation":
        pivot = DB.XYZ(*row["point"])
        axis = DB.Line.CreateBound(pivot, pivot + DB.XYZ.BasisZ)
        angle = rotation_delta(row["rotation_rad"], reference["rotation_rad"])
        DB.ElementTransformUtils.RotateElement(document, element.Id, axis, angle)
        return

    if operation == "nearest_level":
        destination = levels[expected["level_uid"]]
        level = get(document, destination["id"])
        parameter = element.get_Parameter(DB.BuiltInParameter.FAMILY_LEVEL_PARAM)
        if parameter is None or parameter.IsReadOnly or key(parameter.Id) != row["offset_contract"]["level_parameter_id"]:
            raise ValueError("The reviewed level parameter is no longer writable.")
        if not parameter.Set(level.Id):
            raise ValueError("Revit rejected the reviewed level.")
        document.Regenerate()
        element = get(document, row["id"])

    offset = element.get_Parameter(DB.BuiltInParameter.INSTANCE_ELEVATION_PARAM)
    if (offset is None or offset.IsReadOnly or offset.StorageType != DB.StorageType.Double
            or not offset.Definition.GetDataType().Equals(DB.SpecTypeId.Length)
            or key(offset.Id) != row["offset_contract"]["offset_parameter_id"]):
        raise ValueError("The reviewed length offset is no longer writable.")
    if not offset.Set(expected["offset_ft"]):
        raise ValueError("Revit rejected the reviewed offset.")


def verify(before, after, changes, levels):
    if ({key: value for key, value in before.items() if key != "targets"}
            != {key: value for key, value in after.items() if key != "targets"}):
        raise ValueError("The project, reference, or level evidence changed during the trial.")
    if len(before["targets"]) != len(after["targets"]):
        raise ValueError("Revit did not return every reviewed target.")
    for original, row in zip(before["targets"], after["targets"]):
        expected = changes.get(original["id"])
        if expected is None:
            if row != original:
                raise ValueError("An unchanged target moved during the trial.")
            continue
        if ({key: value for key, value in original.items() if key not in PHYSICAL_FIELDS}
                != {key: value for key, value in row.items() if key not in PHYSICAL_FIELDS}):
            raise ValueError("A target's identity, eligibility, or parameter contract changed.")
        state = read_state(row)
        if (any(abs(actual - target) > POSITION_TOLERANCE for actual, target in zip(state["point"], expected["point"]))
                or abs(rotation_delta(state["rotation_rad"], expected["rotation_rad"])) > ROTATION_TOLERANCE
                or state["level_uid"] != expected["level_uid"]):
            raise ValueError("Revit did not reach the reviewed position, rotation, or level.")
        if expected["offset_ft"] is not None:
            destination = levels.get(expected["level_uid"])
            if (state["offset_ft"] is None or abs(state["offset_ft"] - expected["offset_ft"]) > POSITION_TOLERANCE
                    or row.get("level_parameter_uid") != expected["level_uid"]
                    or (destination and row.get("level_id") != destination["id"])):
                raise ValueError("Revit did not preserve the reviewed level and offset pairing.")


def rollback(transaction, group):
    status = DB.TransactionStatus
    current = transaction.GetStatus()
    if current == status.Started:
        if transaction.RollBack() != status.RolledBack or transaction.GetStatus() != status.RolledBack:
            return False
    elif current not in (status.Uninitialized, status.Committed, status.RolledBack):
        return False
    group_state = group.GetStatus()
    if group_state == status.Started:
        return group.RollBack() == status.RolledBack and group.GetStatus() == status.RolledBack
    return group_state == status.Uninitialized and current == status.Uninitialized


def trial(document, args):
    if (set(args) != {"disposable_fixture", "plan"} or args["disposable_fixture"] is not True
            or document.IsWorkshared or document.IsReadOnly or document.IsModifiable):
        raise ValueError("Use the rollback probe on a saved, writable, non-workshared disposable project.")
    plan = args["plan"]
    if (not isinstance(plan, dict) or plan.get("kind") != "level-alignment-rollback-plan"
            or type(plan.get("version")) is not int or plan["version"] != 1):
        raise ValueError("Supply the dedicated level-alignment rollback plan.")
    request = plan["request"]
    before = read_snapshot(document, request)
    computed = probe_plan(before)
    if computed != plan:
        raise ValueError("The project, reference, targets, or level evidence changed after review.")
    changes = computed["changes"]
    levels = {row["uid"]: row for row in before.get("levels", [])}
    events = []
    rollback_events = []
    closing = False
    invalid = False

    def record(sender, event):
        nonlocal invalid
        try:
            operation = str(event.Operation)
            if not event.GetDocument().Equals(document):
                invalid = True
                return
            added, deleted, modified = event.GetAddedElementIds(), event.GetDeletedElementIds(), event.GetModifiedElementIds()
            if added.Count or deleted.Count or modified.Count > len(changes):
                invalid = True
                return
            ids = sorted((key(item) for item in modified), key=int)
            if closing:
                if (len(rollback_events) >= 8 or operation not in ("TransactionRolledBack", "TransactionGroupRolledBack")
                        or not set(ids).issubset(changes)):
                    invalid = True
                else:
                    rollback_events.append({"operation": operation, "modified": ids})
            elif events or operation != "TransactionCommitted" or list(event.GetTransactionNames()) != [TRANSACTION_NAME]:
                invalid = True
            else:
                events.append(ids)
        except Exception:
            invalid = True

    status = DB.TransactionStatus
    group = DB.TransactionGroup(document, TRANSACTION_NAME)
    transaction = DB.Transaction(document, TRANSACTION_NAME)
    application = document.Application
    failure = None
    measured = None
    restored = False
    regeneration_failed = False
    application.DocumentChanged += record
    try:
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit did not start the rollback probe.")
        for row in before["targets"]:
            if row["id"] in changes:
                update(document, row, changes[row["id"]], request["operation"], levels, before.get("reference"))
        document.Regenerate()
        verify(before, read_snapshot(document, request), changes, levels)
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("Revit did not complete the provisional transaction.")
        if invalid or len(events) != 1 or events[0] != sorted(changes, key=int):
            raise ValueError("Revit did not report exactly the reviewed modified element IDs.")
        after = read_snapshot(document, request)
        verify(before, after, changes, levels)
        measured = {row["id"]: read_state(row) for row in after["targets"] if row["id"] in changes}
    except RegenerationFailedException:
        regeneration_failed = True
        failure = "Revit regeneration failed during the rollback probe."
    except Exception as error:
        failure = str(error) if isinstance(error, ValueError) else "Revit could not complete the rollback probe."
    finally:
        closing = True
        try:
            restored = rollback(transaction, group)
        except Exception:
            restored = False
        finally:
            try:
                application.DocumentChanged -= record
            except Exception:
                restored = False

    if not restored or regeneration_failed:
        return {"status": "transaction_unresolved", "message": failure or "Revit did not confirm rollback."}
    try:
        if read_snapshot(document, request) != before:
            return {"status": "transaction_unresolved", "message": "The baseline changed after rollback."}
    except Exception:
        return {"status": "transaction_unresolved", "message": "Revit could not reread the restored baseline."}
    if failure or invalid:
        return {"status": "stopped", "message": failure or "The rollback change record was incomplete.",
                "rollback_verified": True}
    return {
        "status": "trial_rolled_back",
        "basis_sha256": computed["basis_sha256"],
        "modified_ids": events[0],
        "measurements": measured,
        "rollback_events": rollback_events,
        "rollback_verified": True,
    }


def main(document, args):
    try:
        return trial(document, args)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        return {"status": "stopped", "message": str(error)}
    except Exception as error:
        return {"status": "stopped", "message": "Revit could not prepare the rollback probe.",
                "error_type": type(error).__name__}


if "doc" in globals() and "inputs" in globals():
    result = main(doc, inputs)
