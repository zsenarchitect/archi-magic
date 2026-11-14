"""Measure the native effect of a cleanup selection, then roll it back."""

from uuid import uuid4

from pyrevit.api import DB
from System import Int64
from System.Diagnostics import Stopwatch

from cleanup_preview import build_preview
from read_model_cleanup import check_time, element_id, read_inventory


def install_failure_guard(transaction):
    class CleanupGuard(DB.IFailuresPreprocessor):
        __namespace__ = "ArchiMagic.CleanupProbe." + uuid4().hex

        def __init__(self):
            self.failed = False

        def PreprocessFailures(self, accessor):
            try:
                self.failed = self.failed or len(accessor.GetFailureMessages()) > 0
            except Exception:
                self.failed = True
            return (DB.FailureProcessingResult.ProceedWithRollBack if self.failed
                    else DB.FailureProcessingResult.Continue)

    guard = CleanupGuard()
    options = transaction.GetFailureHandlingOptions()
    try:
        options.SetFailuresPreprocessor(guard)
        options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options)
    finally:
        options.Dispose()
    options = transaction.GetFailureHandlingOptions()
    try:
        if options.GetFailuresPreprocessor() != guard or not options.GetClearAfterRollback():
            raise ValueError("Revit did not install the cleanup failure guard.")
    finally:
        options.Dispose()
    return guard


def rollback(transaction, group):
    try:
        states = DB.TransactionStatus
        if transaction.GetStatus() == states.Started:
            if transaction.RollBack() != states.RolledBack:
                return False
        if transaction.GetStatus() not in (states.Uninitialized, states.RolledBack, states.Committed):
            return False
        if group.GetStatus() == states.Started:
            if group.RollBack() != states.RolledBack:
                return False
        return group.GetStatus() == states.RolledBack
    except Exception:
        return False


def probe(document, request):
    fields = {"mode", "selected_ids", "basis_sha256"}
    if not isinstance(request, dict) or set(request) not in (fields, fields | {"scope"}):
        raise ValueError("Supply a reviewed cleanup selection and its digest.")
    if request["mode"] != "rollback_probe" or not isinstance(request["selected_ids"], list):
        raise ValueError("Use rollback_probe with explicit selected element IDs.")
    if "scope" in request and request["scope"] != "selected":
        raise ValueError("Use selected scope or omit scope for the complete inventory.")
    if document is None or document.IsReadOnly or document.IsModifiable or document.IsWorkshared:
        raise ValueError("Use a writable, non-workshared disposable project without an active transaction.")

    def read_current():
        return (read_inventory(document, request["selected_ids"]) if request.get("scope") == "selected"
                else read_inventory(document))

    baseline = read_current()
    preview = build_preview(baseline, request["selected_ids"])
    selection = preview["cleanup_selection"]
    expected = selection["selected_ids"]
    if (preview["basis_sha256"] != request["basis_sha256"] or selection["probe_candidate_ids"] != expected):
        raise ValueError("The reviewed selection changed or includes items with blocking evidence.")

    name = "Probe Revit cleanup " + uuid4().hex
    application = document.Application
    observed = []

    def record_change(_sender, event):
        try:
            if not event.GetDocument().Equals(document):
                observed.append("other_document")
                return
            if (observed or str(event.Operation) != "TransactionCommitted"
                    or list(event.GetTransactionNames()) != [name]
                    or len(event.GetAddedElementIds()) or len(event.GetModifiedElementIds())):
                observed.append(None)
                return
            deleted = event.GetDeletedElementIds()
            observed.append(sorted((element_id(item) for item in deleted), key=int)
                            if len(deleted) == len(expected) else None)
        except Exception:
            observed.append(None)

    states = DB.TransactionStatus
    group = DB.TransactionGroup(document, name)
    transaction = DB.Transaction(document, name)
    failure = None
    clock = Stopwatch.StartNew()
    try:
        if group.Start() != states.Started:
            raise ValueError("Revit did not start the cleanup probe group.")
        if transaction.Start() != states.Started:
            raise ValueError("Revit did not start the cleanup probe transaction.")
        guard = install_failure_guard(transaction)
        application.DocumentChanged += record_change
        try:
            for target in selection["elements"]:
                check_time(clock)
                identifier = DB.ElementId(Int64(int(target["id"])))
                element = document.GetElement(identifier)
                if (element is None or str(element.UniqueId) != target["uid"]
                        or str(element.VersionGuid) != target["version_guid"] or element.Pinned
                        or len(element.GetDependentElements(None))):
                    raise ValueError("A selected element changed before deletion.")
                deleted = document.Delete(identifier)
                if len(deleted) != 1 or element_id(next(iter(deleted))) != target["id"]:
                    raise ValueError("Revit returned a deletion outside the approved scope.")
                check_time(clock)
            state = transaction.Commit()
        finally:
            application.DocumentChanged -= record_change
        if guard.failed or state != states.Committed or transaction.GetStatus() != states.Committed:
            raise ValueError("Revit reported a failure or did not finish the inner transaction.")
        if observed != [expected]:
            raise ValueError("Revit did not attribute exactly the selected deletions to the probe.")
    except Exception as error:
        failure = {"error_type": type(error).__name__}
        if type(error) is ValueError:
            failure["message"] = str(error)
    finally:
        rolled_back = rollback(transaction, group)

    try:
        restored = rolled_back and read_current() == baseline
    except Exception:
        restored = False
    if not restored:
        return {"status": "transaction_unresolved", "message": "Check the probe transaction and project in Revit."}
    if "other_document" in observed:
        return {"status": "transaction_unresolved", "message": "Revit reported changes in another document."}
    if failure is not None:
        return {"status": "stopped", "rollback_verified": True, **failure}
    return {"status": "probe_rolled_back", "rollback_verified": True, "basis_sha256": preview["basis_sha256"],
            "document": selection["document"], "temporary_deleted_ids": expected, "transaction": name}


if "inputs" in globals():
    try:
        result = probe(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "transaction_unresolved", "error_type": type(error).__name__}
