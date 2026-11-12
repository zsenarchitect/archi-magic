"""Load an exact batch of sealed family transfers under mandatory rollback."""

from contextlib import ExitStack
import os
from uuid import uuid4

from family_parameter_reload_effects import verify_loaded_family
from family_parameter_reload_preview import baseline_record, build_preview, project_families, read_baseline
from family_parameter_transfer_export import verified_export
from family_transfer_nested import nested_names
from probe_batch_family_derivation import effect_record
from probe_family_placement import rollback
from read_family_library import WorkerUnresolved
from read_family_placement import eid, inventory, inventory_record

TRANSACTION_NAME = "Trial project family parameter reload"


def reload_options(DB, callbacks):
    class PreserveProjectValues(DB.IFamilyLoadOptions):
        __namespace__ = "ArchiMagic.FamilyTransfer.Reload" + uuid4().hex

        def OnFamilyFound(self, family_in_use, overwrite_values):
            callbacks.append("ordinary")
            return len(callbacks) == 1, False

        def OnSharedFamilyFound(self, shared_family, family_in_use, source, overwrite_values):
            callbacks.append("shared")
            return False, DB.FamilySource.Project, False

    return PreserveProjectValues()


def load_targets(document, DB, baseline, bound):
    expected_names = baseline["families"]
    loaded = {}
    for path, sealed in bound:
        if project_families(document, DB) != expected_names:
            raise ValueError("Loaded family identities changed before a reload.")
        if nested_names(sealed["nested_families"]) & {row["name"].casefold() for row in expected_names.values()}:
            raise ValueError("A nested family could receive the ordinary reload callback.")
        callbacks = []
        success, family = document.LoadFamily(os.fspath(path), reload_options(DB, callbacks))
        if callbacks != ["ordinary"] or not success or family is None:
            raise ValueError("Revit did not complete one unambiguous ordinary family reload.")
        identity = {"id": eid(family), "uid": str(family.UniqueId)}
        if (identity != sealed["loaded_family"] or str(family.Name) != sealed["family"]
                or family.FamilyCategory is None or eid(family.FamilyCategory) != sealed["category_id"]):
            raise ValueError("Revit returned a different family from the reviewed reload target.")
        loaded[identity["id"]] = family
    if project_families(document, DB) != expected_names:
        raise ValueError("A reload changed project family identities or added a nested definition.")
    return loaded


def review_effects(baseline, original, current, events):
    if len(events) != 1 or original.keys() != current.keys():
        raise ValueError("The reload did not expose one complete transaction with unchanged element identities.")
    event = events[0]
    changed = {key for key, value in original.items() if value != current[key]}
    modified = set(event["modified"])
    allowed = set()
    for target in baseline["targets"]:
        scope = {target["loaded_family"]["id"], *(row["id"] for row in target["project_values"]["types"])}
        if not scope <= original.keys() or not modified & scope:
            raise ValueError("Revit did not report effects for an exact reviewed family and its types.")
        allowed.update(scope)
    if (event["added"] or event["deleted"] or modified - allowed or changed - modified
            or any(original[key][0] != current[key][0] for key in changed)):
        raise ValueError("The reload changed project elements outside the reviewed family and type scopes.")
    return event


def inspect_batch(ui, DB, document, baseline, bound, loaded):
    current = inventory(document, DB)
    values = []
    for original, (_, sealed) in zip(baseline["targets"], bound):
        family = loaded[original["loaded_family"]["id"]]
        values.append(verify_loaded_family(ui, DB, document, family, original, sealed))
    if (inventory(document, DB) != current or project_families(document, DB) != baseline["families"]
            or document.IsModifiable):
        raise WorkerUnresolved("The project changed during provisional inspection.")
    return values, current


def execute(ui, DB, document, baseline, bound, original, *, handoff=None):
    name = TRANSACTION_NAME + " " + uuid4().hex
    events = []
    invalid = False
    closing = False

    def record(sender, event):
        nonlocal invalid
        if closing:
            return
        try:
            if (events or not event.GetDocument().Equals(document) or str(event.Operation) != "TransactionCommitted"
                    or list(event.GetTransactionNames()) != [name]):
                invalid = True
            else:
                events.append(effect_record(event))
        except Exception:
            invalid = True

    status = DB.TransactionStatus
    group = DB.TransactionGroup(document, name)
    transaction = DB.Transaction(document, name)
    application = document.Application
    subscribed = handed_off = restored = False
    failure = None
    unresolved = False
    observed = effects = current = None
    try:
        application.DocumentChanged += record
        subscribed = True
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit did not start the project rollback trial.")
        loaded = load_targets(document, DB, baseline, bound)
        document.Regenerate()
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("Revit did not provisionally commit the family reload batch.")
        observed, current = inspect_batch(ui, DB, document, baseline, bound, loaded)
        if invalid:
            raise ValueError("Revit reported an unexpected provisional transaction.")
        effects = review_effects(baseline, original, current, events)
        if handoff is not None:
            handoff(observed, effects)
            if group.Assimilate() != status.Committed or group.GetStatus() != status.Committed:
                raise ValueError("Revit did not transfer the reviewed batch to the approval prompt.")
            handed_off = True
    except Exception as error:
        unresolved = isinstance(error, WorkerUnresolved) or type(error).__name__ == "RegenerationFailedException"
        failure = str(error) if isinstance(error, (ValueError, WorkerUnresolved)) else type(error).__name__
    finally:
        closing = True
        try:
            restored = handed_off or rollback(transaction, group, status)
        except Exception:
            restored = False
        finally:
            if subscribed:
                try:
                    application.DocumentChanged -= record
                except Exception:
                    restored = False
            for scope in (transaction, group):
                try:
                    scope.Dispose()
                except Exception:
                    restored = False
    if not restored or unresolved:
        return {"status": "transaction_unresolved", "reason": failure or "Revit did not confirm batch rollback."}
    if failure:
        return {"status": "stopped", "reason": failure}
    return {"status": "awaiting_bridge_decision" if handed_off else "trial_rolled_back",
            "transaction": name, "families": observed, "effects": effects, "inventory": inventory_record(current)}


def run_trial(ui, DB, document, args, *, handoff=None):
    if args["disposable_fixture"] is not True:
        raise ValueError("Use a saved standalone disposable project for this reload.")
    requested = baseline_record(args["baseline"])
    receipts = [row["receipt"] for row in args["preview"]["rows"]]
    if build_preview(requested, receipts) != args["preview"]:
        raise ValueError("The reload preview does not match the reviewed project and sealed exports.")
    query = {"revit_build": requested["revit_build"], "targets": [row["loaded_family"] for row in requested["targets"]]}
    if read_baseline(ui, DB, document, query) != requested:
        raise ValueError("The project or loaded family evidence changed after review.")
    original = inventory(document, DB)
    if inventory_record(original) != requested["inventory"]:
        raise ValueError("The project inventory changed before the trial.")

    with ExitStack() as stack:
        bound = []
        for row in args["preview"]["rows"]:
            receipt = row["receipt"]
            path, current = stack.enter_context(verified_export(receipt["workspace"], receipt["request_sha256"],
                                                                expected_derived_sha256=receipt["derived_sha256"]))
            if current != receipt:
                raise ValueError("The sealed family changed before reload.")
            bound.append((path, current))
        result = execute(ui, DB, document, requested, bound, original, handoff=handoff)
    if handoff is not None:
        if result["status"] != "awaiting_bridge_decision":
            raise ValueError(result.get("reason", "The reload did not reach the native decision."))
    elif result["status"] != "transaction_unresolved":
        try:
            restored = read_baseline(ui, DB, document, query) == requested and inventory(document, DB) == original
        except Exception:
            restored = False
        if not restored:
            return {"status": "transaction_unresolved", "reason": "The exact project baseline was not restored."}
        result["rollback_verified"] = True
    return dict(result, preview_sha256=args["preview"]["preview_sha256"], baseline_sha256=requested["baseline_sha256"])


try:
    active_project, query_inputs, current_ui = doc, inputs, uiapp
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = run_trial(current_ui, DB, active_project, query_inputs)
    except WorkerUnresolved as error:
        result = {"status": "transaction_unresolved", "reason": str(error)}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "stopped", "reason": str(error)}
