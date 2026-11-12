"""Exercise a reviewed MEP text fill inside an always-rolled-back Revit group."""

import ntpath

from Autodesk.Revit.Exceptions import RegenerationFailedException
from pyrevit.api import DB
from System.Diagnostics import Stopwatch

from duplicate_views import capture_changes, rollback
from mep_parameter_repair_preview import TRANSACTION_NAME, preview
from mep_route_trial_preflight import installed_reader
from view_duplicate_preview import read_payload
from view_filter_transfer_preview import checked_record


def trial(document, args, reader=None):
    if set(args) != {"disposable_fixture", "query", "fill", "preview"} or args["disposable_fixture"] is not True:
        raise ValueError("Confirm a disposable fixture and supply the exact reviewed query, fill and preview.")
    path = document.PathName
    if (not path or not ntpath.isabs(path) or path.startswith(("\\", "//"))
            or not ntpath.splitdrive(path)[0] or ntpath.splitext(path)[1].lower() != ".rvt"
            or document.IsModifiable or str(document.Application.VersionNumber) != "2026"):
        raise ValueError("Open a clean standalone Revit 2026 fixture from a local RVT path.")
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
    events = []
    unverified_document = False

    def on_change(_sender, event):
        nonlocal unverified_document
        try:
            if not event.GetDocument().Equals(document):
                unverified_document = True
                return
            if events or str(event.Operation) != "TransactionCommitted" or list(event.GetTransactionNames()) != [TRANSACTION_NAME]:
                events[:] = [None]
                return
            added, modified, deleted = (event.GetAddedElementIds(), event.GetModifiedElementIds(),
                                        event.GetDeletedElementIds())
            if added.Count or deleted.Count or modified.Count != len(selected):
                events.append(None)
                return
            events.append((set(), {str(item.Value) for item in modified}, set()))
        except Exception:
            unverified_document = True
            events[:] = [None]

    group = DB.TransactionGroup(document, TRANSACTION_NAME)
    transaction = DB.Transaction(document, TRANSACTION_NAME)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the trial group."}
    app = document.Application
    measured, failure, regeneration_failed = None, None, False
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the text-fill transaction.")
        app.DocumentChanged += on_change
        try:
            for row in baseline["elements"]:
                if row["id"] in selected:
                    scope = {"guid": request["guid"], "elements": [{"id": row["id"], "uid": row["uid"]}]}
                    if reader.read_repair_parameters(document, scope, clock)["elements"] != [row]:
                        raise ValueError("A reviewed parameter changed before its setter.")
                    element = document.GetElement(reader.element_id(row["id"]))
                    if not element.get_Parameter(reader.Guid(request["guid"])).Set(text):
                        raise ValueError("Revit did not set a reviewed text parameter.")
            if (transaction.Commit() != DB.TransactionStatus.Committed
                    or transaction.GetStatus() != DB.TransactionStatus.Committed):
                raise ValueError("Revit did not commit the inner trial transaction.")
        finally:
            app.DocumentChanged -= on_change
        changes = capture_changes(events)
        if unverified_document or set(changes["modified"]) != selected or changes["added"] or changes["deleted"]:
            raise ValueError("Revit reported effects outside the reviewed parameter fill.")
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
    return {"status": "mep_text_fill_trial_rolled_back", "basis_sha256": display["basis_sha256"],
            "source_run_id": display["source_run_id"], "trial_changes": measured}


if "inputs" in globals():
    try:
        result = trial(doc, inputs)
    except (AttributeError, KeyError, TypeError, ValueError, OSError, ImportError, RecursionError) as error:
        result = {"status": "stopped", "message": str(error)}
