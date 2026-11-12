# -*- coding: utf-8 -*-
"""Exercise an approved view filter transfer and roll it back on a disposable model."""

import json

from pyrevit.api import DB

from duplicate_views import can_edit, capture_changes, identifier, key, owner_state, rollback
from read_view_filter_graphics import main as read_inventory
from view_filter_transfer_preview import compute

TRANSACTION_NAME = "Test reviewed view filter transfer"


def request_for(plan):
    scope = plan["scope"]
    return {"source_id": scope["source_id"], "allow_placed": scope["allow_placed"],
            "target_ids": [row["requested_id"] for row in scope["targets"]]}


def read_snapshot(document, request):
    return json.loads(read_inventory(document, {"request": request})["inventory_json"])


def approved_snapshot(document, plan, display):
    request = request_for(plan)
    snapshot = read_snapshot(document, request)
    expected_plan, expected_display = compute(snapshot)
    displayed = dict(display)
    notes = displayed.pop("notes", [])
    ownership = displayed.pop("ownership", None)
    if (expected_plan["blocked"] or plan != expected_plan or displayed != expected_display
            or not all(note.startswith("Ownership: ") for note in notes)
            or (ownership and (ownership["owned_by_others"] or ownership["updated_in_central"]))):
        raise ValueError("The plan, displayed preview or live model no longer matches the reviewed transfer.")
    return request, snapshot


def get_settings(view, native_id):
    current = view.GetFilterOverrides(native_id)
    try:
        return DB.OverrideGraphicSettings(current)
    finally:
        current.Dispose()


def set_filters(document, source_view, step):
    target = document.GetElement(identifier(step["recipient_id"]))
    current = {row["id"]: row for row in step["before"]}
    current_order = [row["id"] for row in step["before"]]
    expected_order = [row["id"] for row in step["after"]]
    source_ids = {key(item) for item in source_view.GetOrderedFilters()}
    reorder = current_order != expected_order[:len(current_order)]
    settings = {}
    try:
        # Capture independent native copies before removing any associations.
        for row in step["after"]:
            filter_id = row["id"]
            if reorder or current.get(filter_id) != row:
                native_id = identifier(filter_id)
                owner = source_view if filter_id in source_ids else target
                settings[filter_id] = get_settings(owner, native_id)
        if reorder:
            for filter_id in current_order:
                target.RemoveFilter(identifier(filter_id))
        for row in step["after"]:
            filter_id = row["id"]
            if filter_id not in settings:
                continue
            native_id = identifier(filter_id)
            if reorder or filter_id not in current:
                target.AddFilter(native_id)
            target.SetFilterOverrides(native_id, settings[filter_id])
            target.SetFilterVisibility(native_id, row["visible"])
            target.SetIsFilterEnabled(native_id, row["enabled"])
    finally:
        for item in settings.values():
            item.Dispose()


def verify_state(expected, observed, steps):
    desired = {view_id: step["after"] for step in steps for view_id in step["affected_ids"]}
    for field in ("kind", "document", "scope", "filter_definitions", "pattern_definitions"):
        if expected[field] != observed[field]:
            raise ValueError("Revit changed the reviewed filter scope, model identity or definitions.")
    if set(expected["views"]) != set(observed["views"]):
        raise ValueError("Revit changed the reviewed view inventory.")
    borrowing = []
    for view_id in sorted(expected["views"], key=int):
        before, after = expected["views"][view_id], observed["views"][view_id]
        predicted = dict(before, filters=desired.get(view_id, before["filters"]))
        if any(predicted[field] != after[field] for field in predicted if field not in ("ownership", "writable")):
            raise ValueError("View %s differs from its reviewed filter state." % view_id)
        if after["ownership"] != before["ownership"]:
            borrowing.append({"view_id": view_id, "before": before["ownership"], "after": after["ownership"]})
        if view_id in desired and not after["writable"]:
            raise ValueError("A modified view no longer has a verified editable worksharing state.")
    return borrowing


def probe(document, args):
    if args.get("disposable_fixture") is not True or type(args.get("allow_borrowing", False)) is not bool:
        raise ValueError("Use an explicitly confirmed disposable fixture for this rollback-only probe.")
    plan, display = args["plan"], args["preview"]
    request, snapshot = approved_snapshot(document, plan, display)
    if not plan["steps"]:
        return {"status": "no_changes"}
    allowed = set(plan["ownership_ids"])
    required = {step["recipient_id"] for step in plan["steps"]}
    for view_id in allowed:
        view = document.GetElement(identifier(view_id))
        owner = owner_state(document, view)
        if owner != snapshot["views"][view_id]["ownership"] or not can_edit(owner):
            raise ValueError("Worksharing status changed for view %s." % view_id)
        if owner and owner[0] == "NotOwned" and not args.get("allow_borrowing", False):
            raise ValueError("Review and explicitly approve potential borrowing before running this probe.")

    application, events, foreign_events = document.Application, [], []

    def record(_sender, event):
        try:
            if not event.GetDocument().Equals(document):
                foreign_events[:] = [True]
            if events or foreign_events or list(event.GetTransactionNames()) != [TRANSACTION_NAME]:
                events[:] = [None]
                return
            added, modified, deleted = event.GetAddedElementIds(), event.GetModifiedElementIds(), event.GetDeletedElementIds()
            if added.Count or deleted.Count or modified.Count > len(allowed):
                events.append(None)
                return
            events.append((set(), {key(item) for item in modified}, set()))
        except Exception:
            events[:] = [None]

    group = DB.TransactionGroup(document, TRANSACTION_NAME)
    transaction = DB.Transaction(document, TRANSACTION_NAME)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the rollback group."}
    changes, trial_borrowing, failure = None, [], None
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the trial transaction.")
        application.DocumentChanged += record
        try:
            source = document.GetElement(identifier(snapshot["scope"]["source_authority_id"]))
            for step in plan["steps"]:
                set_filters(document, source, step)
            document.Regenerate()
            state = transaction.Commit()
        finally:
            application.DocumentChanged -= record
        if state != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not finish the trial transaction.")
        changes = capture_changes(events)
        if not required.issubset(changes["modified"]) or not set(changes["modified"]).issubset(allowed):
            raise ValueError("Revit reported modified views outside the reviewed change set or omitted a recipient.")
        trial_borrowing = verify_state(snapshot, read_snapshot(document, request), plan["steps"])
    except Exception as error:
        failure = str(error)
    if not rollback(transaction, group):
        return {"status": "transaction_unresolved", "message": "Revit did not confirm rollback. Reconcile the fixture."}
    try:
        remaining_borrowing = verify_state(snapshot, read_snapshot(document, request), [])
    except Exception as error:
        return {"status": "transaction_unresolved", "message": "Rollback readback failed: " + str(error)}
    if foreign_events:
        return {"status": "transaction_unresolved",
                "message": "A change was reported in another document. Reconcile both fixtures."}
    result = {"status": "trial_rolled_back" if failure is None else "stopped_and_rolled_back",
              "basis_sha256": plan["basis_sha256"], "document": plan["document"], "changes": changes,
              "trial_ownership_changes": trial_borrowing, "remaining_ownership_changes": remaining_borrowing}
    if failure is not None:
        result["message"] = failure
    return result


if "inputs" in globals():
    try:
        result = probe(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception:
        result = {"status": "transaction_unresolved", "message": "Revit could not verify the rollback-only probe."}
