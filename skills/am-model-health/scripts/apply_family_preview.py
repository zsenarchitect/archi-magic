# -*- coding: utf-8 -*-
"""Apply one reviewed thumbnail view to the active staged Revit family."""

import json
import os
import sys

from pyrevit.api import DB
from System import Int64

import read_family_preview as reader

scripts = os.path.dirname(os.path.abspath(reader.__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(scripts)), "am-safe-change", "scripts"))
import family_preview_plan as preview


def change_view(document, checked):
    desired = checked["proposed"]
    step = checked["steps"][0]
    identifier = DB.ElementId(Int64(int(desired["view_type"]["id"])))
    view_type = document.GetElement(identifier)
    if not isinstance(view_type, DB.ViewFamilyType) or str(view_type.UniqueId) != desired["view_type"]["uid"]:
        raise ValueError("The approved 3D view type changed.")
    if step["action"] == "create":
        view = DB.View3D.CreateIsometric(document, identifier)
        view.Name = desired["name"]
    else:
        view = document.GetElement(step["view_uid"])
    if (not isinstance(view, DB.View3D) or view.IsTemplate or view.IsPerspective or view.IsLocked
            or view.CropBoxActive or view.IsSectionBoxActive or reader.id_value(view.ViewTemplateId) != "-1"):
        raise ValueError("The preview view is not an unlocked orthographic view without overrides.")
    if not all(hasattr(view, method) for method in ("CanSaveOrientation", "SaveOrientation", "GetSavedOrientation")):
        raise ValueError("This Revit build cannot verify the unlocked saved orientation.")

    camera = desired["orientation"]
    direction = DB.ViewOrientation3D(DB.XYZ(*camera["eye_ft"]), DB.XYZ(*camera["up"]),
                                     DB.XYZ(*camera["forward"]))
    try:
        view.SetOrientation(direction)
        if not view.CanSaveOrientation():
            raise ValueError("Revit cannot persist the reviewed orientation.")
        view.SaveOrientation()
    finally:
        direction.Dispose()
    view.DisplayStyle = DB.DisplayStyle.Shading
    gray = DB.Color(*preview.GRAY)
    background = DB.ViewDisplayBackground.CreateGradient(gray, gray, gray)
    try:
        view.SetBackground(background)
    finally:
        background.Dispose()

    settings = document.GetDocumentPreviewSettings()
    try:
        if not settings.IsViewIdValidForPreview(view.Id):
            raise ValueError("Revit cannot use the dedicated view for the family thumbnail.")
        settings.PreviewViewId = view.Id
        settings.ForceViewUpdate(True)
    finally:
        settings.Dispose()
    return view


def apply(document, plan):
    identity = plan["basis"]["identity"]
    actual = reader.read_family(document, identity)
    checked = preview.checked_plan(plan, actual)
    transaction = DB.Transaction(document, "Standardize family thumbnail")
    if transaction.Start() != DB.TransactionStatus.Started:
        raise RuntimeError("Revit did not start the thumbnail transaction.")
    try:
        view = change_view(document, checked)
        document.Regenerate()
        settings = document.GetDocumentPreviewSettings()
        try:
            if reader.id_value(settings.PreviewViewId) != reader.id_value(view.Id):
                raise ValueError("Revit did not retain the approved preview view ID.")
            forced = bool(settings.IsViewUpdateForced)
        finally:
            settings.Dispose()
        state = reader.view_state(view)
        preview.check_view_result(checked, actual["identity"], state, forced)
        receipt = {"identity": actual["identity"], "preview_view": state, "force_preview_update": forced}
        encoded = json.dumps(receipt, ensure_ascii=True, allow_nan=False)
        if transaction.Commit() != DB.TransactionStatus.Committed:
            raise RuntimeError("Revit did not commit the thumbnail transaction.")
    except Exception:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        if transaction.GetStatus() != DB.TransactionStatus.RolledBack:
            raise RuntimeError("Check the Revit transaction state before continuing.")
        raise
    return {"status": "ok", "receipt_json": encoded}


if "doc" in globals() and "inputs" in globals():
    result = apply(doc, inputs["preview"])
