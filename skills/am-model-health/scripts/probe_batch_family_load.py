"""Trial exact batch family loading in a disposable project, with mandatory rollback."""

from contextlib import ExitStack
import os
import re
from uuid import uuid4

from batch_family_export import verified_export
from batch_family_load_preview import build_load_preview
from family_library_discovery import manifest_digest
from probe_family_placement import reject_family_conflicts, rollback
from read_batch_family_types import read_project
from read_family_placement import eid, inventory, inventory_record
from probe_batch_family_derivation import effect_record

TRANSACTION_NAME = "Trial batch family load"


def load_exact_types(document, DB, bound, original):
    callbacks = []
    options = reject_family_conflicts(DB, callbacks)
    rows = {}
    families = {}
    added = set()
    for path, group in bound:
        receipt = group["receipt"]
        target_family = None
        target_ids = []
        for row in group["rows"]:
            loaded, symbol = document.LoadFamilySymbol(os.fspath(path), row["type"], options)
            if callbacks:
                raise ValueError("Revit requested an ordinary or shared family replacement. The trial rejected it.")
            if not loaded or symbol is None:
                raise ValueError(f"Revit did not load the reviewed type {row['type']}.")
            family = symbol.Family
            family_id, symbol_id = eid(family), eid(symbol)
            identity = {"id": family_id, "uid": str(family.UniqueId)}
            if (str(symbol.Name) != row["type"] or str(family.Name) != receipt["family"]
                    or eid(family.FamilyCategory) != receipt["category_id"]
                    or family_id == symbol_id or family_id in original or symbol_id in original or symbol_id in added):
                raise ValueError("The loaded family or symbol differs from the reviewed new identity.")
            if target_family is None:
                if family_id in added:
                    raise ValueError("Distinct derived exports resolved to the same new project family.")
                target_family = identity
                added.add(family_id)
            elif target_family != identity:
                raise ValueError("The second type did not join the exact family created in this batch.")
            target_ids.append(symbol_id)
            added.add(symbol_id)
            rows[row["line"]] = {"family_id": family_id, "family_uid": identity["uid"],
                                 "symbol_id": symbol_id, "symbol_uid": str(symbol.UniqueId)}
        if {str(value.Value) for value in family.GetFamilySymbolIds()} != set(target_ids):
            raise ValueError("The loaded family contains types outside the exact requested batch.")
        families[receipt["source_path"]] = target_family
    return rows, families, added


def check_loaded_project(before, after, rows):
    if ({key: value for key, value in before.items() if key != "rows"}
            != {key: value for key, value in after.items() if key != "rows"}
            or len(before["rows"]) != len(after["rows"])):
        raise ValueError("The project identity or batch rows changed during the load.")
    for original, current in zip(before["rows"], after["rows"]):
        key = original["line"]
        if key in rows:
            if (current["line"] != key or current["existing"] != rows[key] or current["differences"]
                    or current["issues"] or current["sizes"] != original["sizes"]):
                raise ValueError("The loaded project type or reviewed size values differ.")
        elif current != original:
            raise ValueError("A skipped existing project type changed during the load.")


def execute_trial(document, DB, args, before, bound, *, handoff=None, expected_state=None):
    original = inventory(document, DB)
    digest = manifest_digest(before)
    baseline_inventory = inventory_record(original)
    if expected_state and (digest != expected_state["baseline_sha256"]
                           or baseline_inventory != expected_state["baseline_inventory"]):
        raise ValueError("The project changed after the verified trial.")
    events = []
    closing = False
    invalid = False
    name = TRANSACTION_NAME + " " + uuid4().hex

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
    failure = None
    regeneration_failed = False
    restored = False
    handed_off = False
    application.DocumentChanged += record
    try:
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit did not start the batch rollback trial.")
        rows, families, added = load_exact_types(document, DB, bound, original)
        document.Regenerate()
        check_loaded_project(before, read_project(document, DB, args["lookup"], require_unmodified=False), rows)
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("Revit did not provisionally commit the batch trial.")
        current = inventory(document, DB)
        if (invalid or len(events) != 1 or set(events[0]["added"]) != added or events[0]["deleted"]
                or set(events[0]["modified"]) - added or set(current) - set(original) != added
                or any(current.get(key) != value for key, value in original.items())):
            raise ValueError("The batch had effects outside its exact new families and types.")
        check_loaded_project(before, read_project(document, DB, args["lookup"], require_unmodified=False), rows)
        if handoff is not None:
            handoff()
            if group.Assimilate() != status.Committed or group.GetStatus() != status.Committed:
                raise ValueError("Revit did not transfer the batch to the guarded approval prompt.")
            handed_off = True
    except Exception as error:
        regeneration_failed = type(error).__name__ == "RegenerationFailedException"
        failure = str(error) if isinstance(error, ValueError) else type(error).__name__
    finally:
        closing = True
        try:
            restored = handed_off or rollback(transaction, group, status)
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
        if handoff is not None:
            raise RuntimeError("The batch did not close cleanly. The bridge must roll it back.")
        return {"status": "transaction_unresolved", "reason": failure or "Revit did not confirm rollback."}
    if handed_off:
        return load_record(args, name, digest, baseline_inventory, families, rows, added,
                                status="awaiting_bridge_decision")
    try:
        if read_project(document, DB, args["lookup"], require_unmodified=False) != before or inventory(document, DB) != original:
            return {"status": "transaction_unresolved", "reason": "The project baseline changed after rollback."}
    except Exception as error:
        return {"status": "transaction_unresolved", "reason": type(error).__name__}
    if failure:
        return {"status": "stopped", "reason": failure, "rollback_verified": True}
    return load_record(args, name, digest, baseline_inventory, families, rows, added,
                            status="trial_rolled_back", rollback_verified=True)


def load_record(args, name, digest, baseline_inventory, families, rows, added, **status):
    return {"preview_sha256": args["preview_sha256"], "transaction": name,
            "baseline_sha256": digest, "baseline_inventory": baseline_inventory,
            "baseline_audit_token": args.get("baseline_audit_token"),
            "families": families, "rows": {str(key): value for key, value in rows.items()},
            "effects": {"added": sorted(added, key=int), "modified": [], "deleted": []}, **status}


def run_trial(document, DB, args, *, handoff=None, expected_state=None):
    token = args.get("baseline_audit_token")
    if (set(args) != {"disposable_fixture", "lookup", "preview", "preview_sha256", "staging", "baseline_audit_token"}
            or args["disposable_fixture"] is not True or document.IsModifiable
            or not isinstance(token, str) or not re.fullmatch("[0-9a-f]{32}", token)):
        raise ValueError("Use a saved disposable project, original audit token, and reviewed batch load.")
    preview = args["preview"]
    if manifest_digest(preview) != args["preview_sha256"]:
        raise ValueError("The batch preview changed after review.")
    before = read_project(document, DB, args["lookup"])
    groups = preview["review"]["exports"]
    exports = [group["receipt"]["workspace"] for group in groups]
    if build_load_preview(args["lookup"], before, args["staging"], exports) != preview:
        raise ValueError("The project or sealed exports changed after review.")
    if not groups:
        return {"status": "no_derivation_needed", "skipped": preview["review"]["skipped"]}
    with ExitStack() as stack:
        bound = []
        for workspace, group in zip(exports, groups):
            path, receipt, _ = stack.enter_context(verified_export(workspace, args["staging"]))
            if receipt != group["receipt"]:
                raise ValueError("A sealed export changed before the native load.")
            bound.append((path, group))
        return execute_trial(document, DB, args, before, bound, handoff=handoff, expected_state=expected_state)


try:
    active_project, query_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = run_trial(active_project, DB, query_inputs)
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "stopped", "reason": str(error)}
