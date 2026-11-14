"""Exercise reviewed MEP parameter repairs inside an always-rolled-back Revit group."""

import ntpath

from Autodesk.Revit.Exceptions import RegenerationFailedException
from pyrevit.api import DB
from System.Diagnostics import Stopwatch

from duplicate_views import capture_changes, rollback
from mep_parameter_repair_preview import TRANSACTION_NAME, preview, preview_system_name
from mep_route_trial_preflight import installed_reader
from view_duplicate_preview import read_payload
from view_filter_transfer_preview import checked_record


RENAME_TRANSACTION_NAME = "Test reviewed MEP system rename"


def require_fixture(document):
    path = document.PathName
    if (not path or not ntpath.isabs(path) or path.startswith(("\\", "//"))
            or not ntpath.splitdrive(path)[0] or ntpath.splitext(path)[1].lower() != ".rvt"
            or document.IsModifiable or str(document.Application.VersionNumber) != "2026"):
        raise ValueError("Open a clean standalone Revit 2026 fixture from a local RVT path.")


def trial(document, args, reader=None):
    if "rename_method" in args:
        return rename_trial(document, args, reader)
    if set(args) != {"disposable_fixture", "query", "fill", "preview"} or args["disposable_fixture"] is not True:
        raise ValueError("Confirm a disposable fixture and supply the exact reviewed query, fill and preview.")
    require_fixture(document)
    display = preview(args["query"], args["fill"])
    if display["blocked"] or display != args["preview"]:
        raise ValueError("The trial requires the exact unblocked repair preview.")
    evidence = read_payload(checked_record(args["query"], "query"), "repair_parameters_json")
    reader = reader or installed_reader()
    request = {"guid": evidence["guid"], "elements": [
        {field: row[field] for field in ("id", "uid")} for row in evidence["elements"]
    ]}
    baseline = {field: evidence[field] for field in ("guid", "elements")}
    clock = Stopwatch.StartNew()

    def read_current():
        source, _, identity = reader.resolve_source(document, None, None)
        if identity != evidence["source"]:
            raise ValueError("The active Revit fixture differs from the reviewed project.")
        return reader.read_repair_parameters(source, request, clock)

    if read_current() != baseline:
        raise ValueError("The exact MEP parameter evidence changed after preview.")

    selected = set(display["ownership_ids"])
    text = args["fill"]["text"]
    expected = dict(baseline, elements=[
        dict(row, has_value=True, value=text, state="already_filled") if row["id"] in selected else row
        for row in baseline["elements"]
    ])
    def mutate():
        for row in baseline["elements"]:
            if row["id"] in selected:
                scope = {"guid": request["guid"], "elements": [{"id": row["id"], "uid": row["uid"]}]}
                if reader.read_repair_parameters(document, scope, clock)["elements"] != [row]:
                    raise ValueError("A reviewed parameter changed before its setter.")
                element = document.GetElement(reader.element_id(row["id"]))
                if not element.get_Parameter(reader.Guid(request["guid"])).Set(text):
                    raise ValueError("Revit did not set a reviewed text parameter.")

    outcome = rollback_trial(document, TRANSACTION_NAME, mutate, read_current, baseline, expected, selected, selected)
    if outcome["status"] == "trial_rolled_back":
        outcome.update(status="mep_text_fill_trial_rolled_back", basis_sha256=display["basis_sha256"],
                       source_run_id=display["source_run_id"])
    return outcome


def rename_trial(document, args, reader=None):
    if (set(args) != {"disposable_fixture", "query", "rename_method", "preview"}
            or args["disposable_fixture"] is not True):
        raise ValueError("Confirm a disposable fixture and supply the exact system name query and preview.")
    method = args["rename_method"]
    if method not in ("element_name", "built_in_parameter"):
        raise ValueError("Choose one explicit system-name API candidate to test.")
    require_fixture(document)
    display = preview_system_name(args["query"])
    if display["blocked"] or display != args["preview"]:
        raise ValueError("The trial requires the exact unblocked system name preview.")
    evidence = read_payload(checked_record(args["query"], "query"), "repair_system_name_json")
    baseline = {field: evidence[field] for field in ("system", "proposed_name", "name_conflicts")}
    system = baseline["system"]
    request = {"id": system["id"], "uid": system["uid"], "name": evidence["proposed_name"]}
    expected = dict(baseline, system=dict(system, name=request["name"]))
    reader = reader or installed_reader()
    clock = Stopwatch.StartNew()

    def read_current():
        source, _, identity = reader.resolve_source(document, None, None)
        if identity != evidence["source"]:
            raise ValueError("The active Revit fixture differs from the reviewed project.")
        return reader.read_system_name_repair(source, request, clock)

    if read_current() != baseline:
        raise ValueError("The system name or membership changed after preview.")

    def mutate():
        target = document.GetElement(reader.element_id(request["id"]))
        if str(target.UniqueId) != request["uid"] or target.Name != system["name"]:
            raise ValueError("The reviewed system changed before its setter.")
        if method == "element_name":
            target.Name = request["name"]
        else:
            parameter = target.get_Parameter(DB.BuiltInParameter.RBS_SYSTEM_NAME_PARAM)
            if (parameter is None or parameter.IsReadOnly or parameter.StorageType != DB.StorageType.String
                    or str(parameter.Element.UniqueId) != request["uid"] or parameter.AsString() != system["name"]
                    or not parameter.Set(request["name"])):
                raise ValueError("The built-in system name parameter did not accept the reviewed name.")

    outcome = rollback_trial(document, RENAME_TRANSACTION_NAME, mutate, read_current, baseline, expected,
                             {request["id"]}, set(display["ownership_ids"]))
    if outcome["status"] == "trial_rolled_back":
        outcome.update(status="mep_system_rename_trial_rolled_back", basis_sha256=display["basis_sha256"],
                       source_run_id=display["source_run_id"], rename_method=method)
    return outcome


def rollback_trial(document, transaction_name, mutate, read_current, baseline, expected, required, allowed):
    events = []
    unverified_document = False

    def on_change(_sender, event):
        nonlocal unverified_document
        try:
            if not event.GetDocument().Equals(document):
                unverified_document = True
                return
            if events or str(event.Operation) != "TransactionCommitted" or list(event.GetTransactionNames()) != [transaction_name]:
                events[:] = [None]
                return
            added, modified, deleted = (event.GetAddedElementIds(), event.GetModifiedElementIds(),
                                        event.GetDeletedElementIds())
            if added.Count or deleted.Count or modified.Count > len(allowed):
                events.append(None)
                return
            events.append((set(), {str(item.Value) for item in modified}, set()))
        except Exception:
            unverified_document = True
            events[:] = [None]

    group = DB.TransactionGroup(document, transaction_name)
    transaction = DB.Transaction(document, transaction_name)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the trial group."}
    app = document.Application
    measured, failure, regeneration_failed = None, None, False
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the repair transaction.")
        app.DocumentChanged += on_change
        try:
            mutate()
            if (transaction.Commit() != DB.TransactionStatus.Committed
                    or transaction.GetStatus() != DB.TransactionStatus.Committed):
                raise ValueError("Revit did not commit the inner trial transaction.")
        finally:
            app.DocumentChanged -= on_change
        changes = capture_changes(events)
        modified = set(changes["modified"])
        if (unverified_document or not required <= modified <= allowed or changes["added"] or changes["deleted"]):
            raise ValueError("Revit reported effects outside the reviewed repair.")
        if read_current() != expected:
            raise ValueError("The committed trial values differ from the preview.")
        measured = changes
    except RegenerationFailedException:
        regeneration_failed = True
        failure = "Revit regeneration failed. Reconcile the fixture before retrying."
    except Exception as error:
        failure = str(error)
    finally:
        rolled_back = rollback(transaction, group)

    if regeneration_failed or unverified_document or not rolled_back or group.GetStatus() != DB.TransactionStatus.RolledBack:
        return {"status": "transaction_unresolved", "message": failure or "Revit did not confirm the trial rollback."}
    try:
        if read_current() != baseline:
            raise ValueError("The original parameters differ after rollback.")
    except Exception as error:
        return {"status": "transaction_unresolved", "message": str(error)}
    if failure is not None:
        return {"status": "stopped_and_rolled_back", "message": failure}
    return {"status": "trial_rolled_back", "trial_changes": measured}


if "inputs" in globals():
    try:
        result = trial(doc, inputs)
    except (AttributeError, KeyError, TypeError, ValueError, OSError, ImportError, RecursionError) as error:
        result = {"status": "stopped", "message": str(error)}
