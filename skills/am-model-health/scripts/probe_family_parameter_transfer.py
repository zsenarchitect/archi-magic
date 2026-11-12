"""Trial a reviewed family parameter transfer with mandatory native rollback."""

from contextlib import contextmanager
import json
from time import monotonic
from uuid import uuid4

from family_library_discovery import (
    MAX_FAMILY_BYTES, MAX_SECONDS, directory_info, identity, manifest_digest, pinned_root, root_path,
)
from family_parameter_transfer_effects import add_definitions, verify_element_effects, verify_family_effects
from family_parameter_transfer_preview import preview, reviewed_snapshot
from probe_batch_family_derivation import effect_record
from probe_family_placement import rollback
from read_batch_models import normalized_path
from read_family_library import WorkerUnresolved
from read_family_parameter_transfer import snapshot, transfer_state
from read_family_placement import eid, inventory
from stage_family_library import check_file_record, parts, verify_pair


TRANSACTION_NAME = "Trial family parameter transfer"


@contextmanager
def pinned_copy(record):
    workspace = root_path(record["workspace"])
    basename, = parts(record["file"]["basename"])
    check_file_record(record["file"], basename, MAX_FAMILY_BYTES)
    with pinned_root(workspace) as directory:
        if identity(directory_info(directory)) != record["workspace_identity"]:
            raise ValueError("The reviewed private workspace changed.")
        budget = {"entries": 0, "bytes": 0, "deadline": monotonic() + MAX_SECONDS}
        verify_pair(directory, record["file"], None, budget)
        try:
            yield workspace / basename
        finally:
            try:
                verify_pair(directory, record["file"], None, budget)
            except (OSError, ValueError) as error:
                raise WorkerUnresolved("The private family bytes could not be verified after the trial.") from error


def exact_copy(ui, DB, document, path, target, *, kept=False):
    active = ui.ActiveUIDocument
    if (document is None or active is None or not active.Document.Equals(document)
            or list(ui.Application.Documents) != [document] or document.IsReadOnly
            or not document.PathName or normalized_path(document.PathName) != normalized_path(path)):
        raise ValueError("Open only the reviewed writable private family copy.")
    current = snapshot(document, DB, kept=kept)
    if any(value != target[key] for key, value in current.items() if not kept or key != "state"):
        raise ValueError("The private copy differs from the exact loaded family read.")
    return current["state"]


def family_audit(ui, document, DB, args, *, kept=False):
    reviewed = preview(args["source_read"], args["target_read"], args["request"])
    if reviewed["preview_sha256"] != args["preview_sha256"]:
        raise ValueError("The transfer preview differs from the reviewed preview.")
    target = reviewed_snapshot(args["target_read"], "target", reviewed["target_sha256"])
    with pinned_copy(args["working_copy"]) as path:
        state = exact_copy(ui, DB, document, path, target, kept=kept)
        elements = inventory(document, DB)
        owner = eid(document.OwnerFamily)
        if owner not in elements:
            raise ValueError("The family owner is absent from the element inventory.")
        return {"preview_sha256": reviewed["preview_sha256"], "copy_sha256": manifest_digest(args["working_copy"]),
                "owner": owner, "inventory": elements, "state": state, "kept": kept}


def audit_result(ui, document, DB, args):
    record = family_audit(ui, document, DB, args["transfer"], kept=args.get("kept") is True)
    encoded = json.dumps(record, ensure_ascii=True, allow_nan=False)
    if len(encoded) > 8 * 1024 * 1024:
        raise ValueError("The family audit exceeds 8 MiB.")
    return {"status": "ok", "snapshot_json": encoded, "audit_token": uuid4().hex,
            "trial_transaction": args.get("trial_transaction")}


def run_trial(ui, document, DB, args, *, handoff=None, expected_audit_sha256=None, qualification=None):
    if handoff is not None and qualification is not None:
        raise ValueError("Formula binding qualification requires mandatory rollback.")
    if args["disposable_fixture"] is not True:
        raise ValueError("Use a private disposable family copy.")
    reviewed = preview(args["source_read"], args["target_read"], args["request"])
    if reviewed["preview_sha256"] != args["preview_sha256"]:
        raise ValueError("The transfer preview differs from the reviewed preview.")
    target = reviewed_snapshot(args["target_read"], "target", reviewed["target_sha256"])
    with pinned_copy(args["working_copy"]) as path:
        baseline = exact_copy(ui, DB, document, path, target)
        if not any(row["action"] == "add" for row in reviewed["operations"].values()):
            return {"status": "no_changes", "preview_sha256": reviewed["preview_sha256"]}
        original = inventory(document, DB)
        owner = eid(document.OwnerFamily)
        if owner not in original:
            raise ValueError("The family owner is absent from the element inventory.")
        audit = {"preview_sha256": reviewed["preview_sha256"], "copy_sha256": manifest_digest(args["working_copy"]),
                 "owner": owner, "inventory": original, "state": baseline, "kept": False}
        audit_sha256 = manifest_digest(audit)
        if handoff is not None and audit_sha256 != expected_audit_sha256:
            raise ValueError("The private family baseline changed before replay.")
        result = execute_trial(document, DB, reviewed, baseline, original, owner, handoff=handoff,
                               audit_sha256=audit_sha256, qualification=qualification)
        if handoff is not None:
            if result["status"] != "awaiting_bridge_decision":
                raise ValueError(result.get("reason", "The replay did not reach the native decision."))
            return result
        if result["status"] == "transaction_unresolved":
            return result
        try:
            restored = exact_copy(ui, DB, document, path, target) == baseline and inventory(document, DB) == original
        except Exception:
            restored = False
        if not restored:
            return {"status": "transaction_unresolved", "reason": "The exact family baseline was not restored."}
        return dict(result, rollback_verified=True)


def execute_trial(document, DB, plan, baseline, original, owner, *, handoff=None, audit_sha256=None, qualification=None):
    name = TRANSACTION_NAME + " " + uuid4().hex
    events = []
    invalid = False
    closing = False

    def record(sender, event):
        nonlocal invalid
        if closing:
            return
        try:
            if (not event.GetDocument().Equals(document) or str(event.Operation) != "TransactionCommitted"
                    or list(event.GetTransactionNames()) != [name] or events):
                invalid = True
            else:
                events.append(effect_record(event))
        except Exception:
            invalid = True

    application = document.Application
    group = DB.TransactionGroup(document, name)
    transaction = DB.Transaction(document, name)
    status = DB.TransactionStatus
    failure = None
    regeneration_failed = False
    restored = False
    subscribed = False
    handed_off = False
    observed = None
    additions = None
    qualification_result = None
    try:
        application.DocumentChanged += record
        subscribed = True
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit did not start the private family trial.")
        additions = add_definitions(document, DB, plan, baseline)
        provisional = transfer_state(document, DB, inspect_trial=True)
        verify_family_effects(plan, baseline, provisional, additions)
        if qualification is not None:
            qualification_result = qualification(plan, provisional)
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("Revit did not provisionally commit the family trial.")
        if invalid:
            raise ValueError("Revit reported an unexpected provisional event.")
        observed = verify_family_effects(plan, baseline, transfer_state(document, DB, inspect_trial=True), additions)
        verify_element_effects(original, inventory(document, DB), events, owner, additions)
        if handoff is not None:
            handoff(observed, events, additions, audit_sha256)
            if group.Assimilate() != status.Committed or group.GetStatus() != status.Committed:
                raise ValueError("Revit did not transfer the checked family changes to the bridge.")
            handed_off = True
    except Exception as error:
        regeneration_failed = isinstance(error, WorkerUnresolved) or type(error).__name__ == "RegenerationFailedException"
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

    if not restored or regeneration_failed:
        return {"status": "transaction_unresolved", "reason": failure or "Revit did not confirm rollback."}
    if failure:
        return {"status": "stopped", "reason": failure}
    result = {"status": "awaiting_bridge_decision" if handed_off else "trial_rolled_back",
            "preview_sha256": plan["preview_sha256"], "transaction": name,
              "added_ids": additions, "values": observed, "effects": events, "audit_sha256": audit_sha256}
    if qualification_result is not None:
        result["formula_qualification"] = qualification_result
    return result


try:
    active_family, query_inputs, current_ui = doc, inputs, uiapp
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = run_trial(current_ui, active_family, DB, query_inputs)
    except WorkerUnresolved as error:
        result = {"status": "transaction_unresolved", "reason": str(error)}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "stopped", "reason": str(error)}
