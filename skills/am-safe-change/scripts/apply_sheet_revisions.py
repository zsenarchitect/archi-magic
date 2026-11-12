# -*- coding: utf-8 -*-
"""Apply a reviewed sheet revision preview with the pyRevit Agent Runtime."""

import json

from pyrevit.api import DB
from System.Collections.Generic import List

import sheet_revision_preview as preview
from read_sheets import revision_inventory, revision_scope


def rollback(transaction):
    if transaction.GetStatus() == DB.TransactionStatus.Started:
        transaction.RollBack()
    if transaction.GetStatus() != DB.TransactionStatus.RolledBack:
        raise RuntimeError("Check the Revit transaction state before continuing.")


def apply(document, plan):
    if document is None or document.IsReadOnly:
        raise ValueError("Open the saved, writable project first.")
    request = preview.read_request(plan["basis"])
    current = revision_inventory(document, request)
    steps = preview.approved_steps(plan, current)
    if not steps:
        raise ValueError("The selected sheets already match the preview.")
    targets = revision_scope([row["sheet_id"] for row in steps], 30)

    transaction = DB.Transaction(document, "Update sheet revisions")
    if transaction.Start() != DB.TransactionStatus.Started:
        raise RuntimeError("Revit did not start the sheet revision transaction.")
    try:
        for step in steps:
            sheet = document.GetElement(targets[step["sheet_id"]])
            if sheet.UniqueId != step["sheet_uid"]:
                raise ValueError("A reviewed sheet changed identity.")
            revisions = List[DB.ElementId]()
            for revision in revision_scope(step["additional_revision_ids"], 500).values():
                revisions.Add(revision)
            sheet.SetAdditionalRevisionIds(revisions)
        document.Regenerate()
        after = revision_inventory(document, request)
        preview.verify_result(plan, after)
        encoded = json.dumps(after, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("The sheet revision result exceeds the snapshot size limit.")
        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise RuntimeError("Revit did not commit the sheet revision transaction.")
    except Exception:
        rollback(transaction)
        raise
    return {"status": "ok", "snapshot_json": encoded, "steps_applied": len(steps)}


if "doc" in globals() and "inputs" in globals():
    plan = preview.load(inputs["preview_path"]) if "preview_path" in inputs else inputs["preview"]
    result = apply(doc, plan)
