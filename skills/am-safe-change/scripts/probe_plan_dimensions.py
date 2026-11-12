"""Create reviewed plan dimensions temporarily and prove exact rollback in a disposable model."""

from uuid import uuid4

from Autodesk.Revit.Exceptions import RegenerationFailedException
from pyrevit.api import DB

from plan_dimension_preview import (
    TRANSACTION_NAME, compare, existing_signature, geometry_context, probe_plan, proposal_signature,
)
from read_plan_dimensions import get, key, read_dimension, read_snapshot, stable


def create(document, view, dimension_type, item):
    references = DB.ReferenceArray()
    for witness in item["witnesses"]:
        owner = get(document, witness["source_id"])
        reference = DB.Reference.ParseFromStableRepresentation(document, witness["stable_ref"])
        if (owner is None or str(owner.UniqueId) != witness["source_uid"]
                or stable(document, owner, reference) != witness["stable_ref"]):
            raise ValueError("A reviewed dimension reference could not be resolved to the same source.")
        references.Append(reference)
    line = DB.Line.CreateBound(*(DB.XYZ(*point) for point in item["line"]))
    dimension = document.Create.NewDimension(view, line, references, dimension_type)
    if dimension is None:
        raise ValueError("Revit did not create the reviewed dimension.")
    return dimension


def measure(document, view, created, plan, basis):
    geometry = geometry_context(basis)
    frame = geometry[:2]
    measurements = {}
    for row_key, (id_value, uid) in created.items():
        dimension = get(document, id_value)
        if (not isinstance(dimension, DB.Dimension) or str(dimension.UniqueId) != uid
                or key(dimension.OwnerViewId) != key(view.Id) or dimension.IsHidden(view)):
            raise ValueError("The created dimension identity, visibility, or view differs from the plan.")
        observed = read_dimension(document, dimension, frame)
        expected = proposal_signature(plan["proposals"][row_key], geometry)
        if compare(expected, existing_signature(observed, geometry)) != "match":
            raise ValueError("The measured dimension line, type, references, or segment values differ from the plan.")
        measurements[row_key] = observed
    return measurements


def rollback(transaction, group):
    status = DB.TransactionStatus
    state = transaction.GetStatus()
    if state == status.Started:
        if transaction.RollBack() != status.RolledBack or transaction.GetStatus() != status.RolledBack:
            return False
    elif state not in (status.Uninitialized, status.Committed, status.RolledBack):
        return False
    if group.GetStatus() == status.Started:
        return group.RollBack() == status.RolledBack and group.GetStatus() == status.RolledBack
    return group.GetStatus() == status.Uninitialized and state == status.Uninitialized


def trial(document, view, args):
    if (set(args) != {"disposable_fixture", "plan"} or args["disposable_fixture"] is not True
            or document.IsWorkshared or document.IsReadOnly or document.IsModifiable):
        raise ValueError("Use a saved, writable, non-workshared disposable project.")
    plan = args["plan"]
    if (plan.get("kind") != "plan-dimension-rollback-plan" or type(plan.get("version")) is not int
            or plan["version"] != 1):
        raise ValueError("Supply the dedicated plan dimension rollback payload.")
    request = plan["request"]
    baseline = read_snapshot(document, view, request)
    if probe_plan(baseline) != plan:
        raise ValueError("The reviewed plan view, references, or existing dimensions changed.")
    dimension_type = get(document, baseline["dimension_type"]["id"])
    name = TRANSACTION_NAME + " " + uuid4().hex
    created, provisional_ids, events = {}, set(), []
    closing = False
    invalid = False

    def record(sender, event):
        nonlocal invalid
        try:
            if not event.GetDocument().Equals(document):
                invalid = True
                return
            added, modified, deleted = event.GetAddedElementIds(), event.GetModifiedElementIds(), event.GetDeletedElementIds()
            if max(added.Count, modified.Count, deleted.Count) > 64:
                invalid = True
                return
            adds, modifies, deletes = ({key(item) for item in values} for values in (added, modified, deleted))
            operation = str(event.Operation)
            provisional_ids.update(adds)
            if closing:
                if (operation not in ("TransactionRolledBack", "TransactionGroupRolledBack")
                        or adds or modifies or not deletes.issubset(provisional_ids)):
                    invalid = True
            elif (events or operation != "TransactionCommitted" or list(event.GetTransactionNames()) != [name]
                  or modifies or deletes):
                invalid = True
            else:
                events.append(adds)
        except Exception:
            invalid = True

    status = DB.TransactionStatus
    group = DB.TransactionGroup(document, name)
    transaction = DB.Transaction(document, name)
    application = document.Application
    failure = None
    measurements = None
    restored = False
    regeneration_failed = False
    application.DocumentChanged += record
    try:
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit did not start the rollback probe.")
        for row_key, item in plan["proposals"].items():
            dimension = create(document, view, dimension_type, item)
            id_value = key(dimension.Id)
            created[row_key] = (id_value, str(dimension.UniqueId))
            provisional_ids.add(id_value)
        document.Regenerate()
        measure(document, view, created, plan, baseline)
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("Revit did not complete the provisional transaction.")
        approved_ids = {id_value for id_value, _ in created.values()}
        if invalid or events != [approved_ids] or len(approved_ids) != len(plan["proposals"]):
            raise ValueError("The exact Revit change record differs from the reviewed dimension additions.")
        measurements = measure(document, view, created, plan, baseline)
        provisional = read_snapshot(document, view, request)
        provisional["existing"] = [row for row in provisional["existing"] if row["id"] not in approved_ids]
        if provisional != baseline:
            raise ValueError("Revit changed the sources or existing dimensions during the probe.")
    except RegenerationFailedException:
        regeneration_failed = True
        failure = "Revit regeneration failed during the dimension probe."
    except Exception as error:
        failure = str(error) if isinstance(error, ValueError) else "Revit could not complete the dimension probe."
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
        if (any(get(document, id_value) is not None for id_value in provisional_ids)
                or read_snapshot(document, view, request) != baseline):
            return {"status": "transaction_unresolved", "message": "Revit did not restore the reviewed baseline."}
    except Exception:
        return {"status": "transaction_unresolved", "message": "Revit could not reread the restored baseline."}
    if failure or invalid:
        return {"status": "stopped", "message": failure or "The rollback change record was incomplete.",
                "rollback_verified": True}
    return {
        "status": "trial_rolled_back", "rollback_verified": True, "basis_sha256": plan["basis_sha256"],
        "transaction": name, "revit": baseline["revit"], "measurements": measurements,
        "temporary_dimension_ids": sorted(provisional_ids, key=int),
    }


def main(document, ui_document, args):
    try:
        if ui_document is None or not document.Equals(ui_document.Document):
            raise ValueError("The UI document does not match the reviewed host project.")
        return trial(document, ui_document.ActiveView, args)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        return {"status": "stopped", "message": str(error)}
    except Exception as error:
        return {"status": "stopped", "message": "Revit could not prepare the dimension rollback probe.",
                "error_type": type(error).__name__}


if "doc" in globals() and "uidoc" in globals() and "inputs" in globals():
    result = main(doc, uidoc, inputs)
