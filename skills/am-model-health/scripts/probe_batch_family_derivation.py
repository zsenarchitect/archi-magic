"""Qualify reviewed type derivation with mandatory rollback in an active private family."""

import math
from uuid import uuid4

from batch_family_derivation_preview import build_preview, working_read
from family_library_discovery import manifest_digest
from prepare_batch_family_derivation import verified_working_copy
from probe_family_placement import rollback
from read_batch_family_derivation import family_state, read_working_family
from read_batch_family_types import internal_size
from read_family_placement import inventory, inventory_record

TRANSACTION_NAME = "Trial batch family type derivation"


def add_types(document, DB, plan, baseline):
    manager = document.FamilyManager
    seeds = {str(family_type.Name): family_type for family_type in manager.Types}
    parameters = {str(parameter.Id.Value): parameter for parameter in manager.Parameters}
    for row in plan["rows"]:
        manager.CurrentType = seeds[row["seed_type"]]
        if str(manager.CurrentType.Name) != row["seed_type"]:
            raise ValueError("Revit did not select the exact embedded seed.")
        created = manager.NewType(row["type"])
        if (created is None or str(created.Name) != row["type"] or manager.CurrentType is None
                or str(manager.CurrentType.Name) != row["type"]):
            raise ValueError("Revit did not make the exact new type current.")
        for size in row["sizes"]:
            value = internal_size(DB, size)
            if value != size["internal_value"]:
                raise ValueError("The native unit conversion differs from the reviewed size.")
            manager.Set(parameters[size["definition"]["id"]], value)
        document.Regenerate()
    manager.CurrentType = seeds[baseline["current_type"]]
    document.Regenerate()


def compare_state(plan, baseline, changed):
    if (changed["parameters"] != baseline["parameters"] or changed["current_type"] != baseline["current_type"]
            or any(changed["types"].get(name) != values for name, values in baseline["types"].items())
            or set(changed["types"]) - set(baseline["types"]) != {row["type"] for row in plan["rows"]}):
        raise ValueError("The trial changed existing types or parameter definitions, or created unexpected types.")
    observed = {}
    for row in plan["rows"]:
        name = row["type"]
        actual = changed["types"][name]
        seed = baseline["types"][row["seed_type"]]
        sizes = {size["definition"]["id"]: size["internal_value"] for size in row["sizes"]}
        if set(actual) != set(seed):
            raise ValueError("The new type does not expose the complete seed parameter set.")
        for key, value in actual.items():
            field = baseline["parameters"][key]
            if key in sizes:
                if (value["state"] != "value" or not math.isclose(value["value"], sizes[key], rel_tol=1e-9, abs_tol=1e-9)):
                    raise ValueError(f"The size parameter {field['name']} did not reach its reviewed value.")
            elif not (field["formula_driven"] or field["reporting"]) and value != seed[key]:
                raise ValueError(f"The new type did not inherit the reviewed seed value for {field['name']}.")
        observed[name] = actual
    return observed


def effect_record(event):
    return {"added": sorted(str(item.Value) for item in event.GetAddedElementIds()),
            "deleted": sorted(str(item.Value) for item in event.GetDeletedElementIds()),
            "modified": sorted(str(item.Value) for item in event.GetModifiedElementIds())}


def validate_effects(original, current, events, owner):
    modified = {key for key in set(original) & set(current) if original[key] != current[key]}
    if (set(original) != set(current) or modified - {owner} or len(events) > 1
            or any(event["added"] or event["deleted"] or set(event["modified"]) - {owner} for event in events)
            or (modified and (not events or not modified.issubset(events[0]["modified"])))
            or current[owner][0] != original[owner][0]):
        raise ValueError("The trial changed elements outside the reviewed family owner.")


def run_trial(document, DB, args):
    if (set(args) != {"disposable_fixture", "derivation", "working_read", "preview", "preview_sha256"}
            or args["disposable_fixture"] is not True):
        raise ValueError("Use the reviewed derivation in an active private disposable family.")
    plan = args["derivation"]
    preview = args["preview"]
    if (manifest_digest(preview) != args["preview_sha256"]
            or build_preview(plan, args["working_read"]) != preview):
        raise ValueError("The derivation preview changed after review.")
    baseline = working_read(args["working_read"])
    current = read_working_family(document, plan, DB)
    if current != baseline:
        raise ValueError("The exact working family changed after review.")
    original = inventory(document, DB)
    if inventory_record(original) != baseline["inventory"] or baseline["owner"]["id"] not in original:
        raise ValueError("The working family inventory changed before the trial.")
    state = baseline["state"]
    if state["current_type"] not in state["types"]:
        raise ValueError("The working family has no exact current seed type.")
    return execute_trial(document, DB, args, plan, baseline, original)


def execute_trial(document, DB, args, plan, baseline, original):
    transaction_name = TRANSACTION_NAME + " " + uuid4().hex
    events = []
    invalid = False
    closing = False

    def record(sender, event):
        nonlocal invalid
        if closing:
            return
        try:
            if (not event.GetDocument().Equals(document) or str(event.Operation) != "TransactionCommitted"
                    or list(event.GetTransactionNames()) != [transaction_name] or events):
                invalid = True
            else:
                events.append(effect_record(event))
        except Exception:
            invalid = True

    with verified_working_copy(plan):
        application = document.Application
        group = DB.TransactionGroup(document, transaction_name)
        transaction = DB.Transaction(document, transaction_name)
        status = DB.TransactionStatus
        failure = None
        regeneration_failed = False
        restored = False
        observed = None
        application.DocumentChanged += record
        try:
            if group.Start() != status.Started or transaction.Start() != status.Started:
                raise ValueError("Revit could not start the derivation trial.")
            add_types(document, DB, plan, baseline["state"])
            compare_state(plan, baseline["state"], family_state(document))
            if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
                raise ValueError("Revit did not provisionally commit the derivation trial.")
            if invalid:
                raise ValueError("Revit reported unexpected provisional events.")
            observed = compare_state(plan, baseline["state"], family_state(document))
            validate_effects(original, inventory(document, DB), events, baseline["owner"]["id"])
        except Exception as error:
            regeneration_failed = type(error).__name__ == "RegenerationFailedException"
            failure = str(error) if isinstance(error, ValueError) else type(error).__name__
        finally:
            closing = True
            try:
                restored = rollback(transaction, group, status)
            except Exception:
                restored = False
            finally:
                try:
                    application.DocumentChanged -= record
                except Exception:
                    restored = False
                for scope in (transaction, group):
                    try:
                        scope.Dispose()
                    except Exception:
                        restored = False
    if not restored or regeneration_failed:
        return {"status": "transaction_unresolved", "reason": failure or "Revit did not confirm rollback."}
    try:
        if read_working_family(document, plan, DB) != baseline or inventory(document, DB) != original:
            return {"status": "transaction_unresolved", "reason": "The working family changed after rollback."}
    except Exception as error:
        return {"status": "transaction_unresolved", "reason": type(error).__name__}
    if failure:
        return {"status": "stopped", "reason": failure, "rollback_verified": True, "effects": events}
    return {"status": "trial_rolled_back", "preview_sha256": args["preview_sha256"],
            "working_read_sha256": baseline["working_read_sha256"], "transaction": transaction_name,
            "types": observed, "effects": events, "rollback_verified": True}


try:
    active_family, query_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = run_trial(active_family, DB, query_inputs)
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "stopped", "reason": str(error)}
