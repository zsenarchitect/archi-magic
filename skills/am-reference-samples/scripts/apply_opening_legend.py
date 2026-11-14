# -*- coding: utf-8 -*-
"""Apply a reviewed door and window elevation legend in one Revit transaction."""

import json

from pyrevit.api import DB
from System.Collections.Generic import List

import opening_legend_preview as preview
import read_opening_types as reader


def snapshot_request(basis):
    legend = basis["legend"]
    layout = legend["layout"]
    return {"legend_id": legend["id"], "source_legend_id": layout["source_legend_id"],
            "seed_component_id": layout["seed"]["id"], "text_type_id": layout["text_type"]["id"]}


def verify_ownership(document, ids):
    if not document.IsWorkshared:
        return
    for value in ids:
        identifier = reader.positive_id(value)
        status = DB.WorksharingUtils.GetCheckoutStatus(document, identifier)
        update = DB.WorksharingUtils.GetModelUpdatesStatus(document, identifier)
        if status == DB.CheckoutStatus.OwnedByOtherUser or update in (
                DB.ModelUpdatesStatus.UpdatedInCentral, DB.ModelUpdatesStatus.DeletedInCentral):
            raise ValueError("A reviewed target is owned by another user or has changed in central.")


def element_pair(document, target, step):
    component_id, note_id = step["existing_ids"]
    component, note = [document.GetElement(reader.positive_id(value)) for value in (component_id, note_id)]
    if (component is None or not isinstance(note, DB.TextNote)
            or reader.element_id(component.Category.Id) != str(int(DB.BuiltInCategory.OST_LegendComponents))):
        raise ValueError("A reviewed component or label is unavailable.")
    allowed = {component_id, note_id}
    for item in (component, note):
        if (item.Pinned or reader.element_id(item.GroupId) != "-1"
                or reader.element_id(item.OwnerViewId) != reader.element_id(target.Id)
                or any(reader.element_id(value) not in allowed for value in item.GetDependentElements(None))):
            raise ValueError("A reviewed legend element is pinned, grouped, or has an unreviewed dependency.")
    return component, note


def retire(document, target, step):
    pair = element_pair(document, target, step)
    ids = List[DB.ElementId]()
    for item in pair:
        ids.Add(item.Id)
    deleted = {reader.element_id(value) for value in document.Delete(ids)}
    if deleted != set(step["existing_ids"]):
        raise ValueError("Revit tried to delete elements outside the reviewed legend pair.")


def get_rectangle(element, target, legend):
    bounds = reader.element_bounds(element, target)
    if bounds is None:
        raise ValueError("Revit did not return bounds for a reviewed legend element.")
    return preview.view_rectangle(bounds, legend)


def translate(document, element, legend, delta_u, delta_v):
    if abs(delta_u) > preview.EPSILON or abs(delta_v) > preview.EPSILON:
        delta = [delta_u * right + delta_v * up for right, up in zip(legend["right"], legend["up"])]
        DB.ElementTransformUtils.MoveElement(document, element.Id, DB.XYZ(*delta))


def arrange(document, target, legend, component, note, cell):
    clearance = legend["scale"] / 96.0
    document.Regenerate()
    note_rect = get_rectangle(note, target, legend)
    translate(document, note, legend, cell[0] + clearance - note_rect[0], cell[1] + clearance - note_rect[1])
    document.Regenerate()
    note_rect = get_rectangle(note, target, legend)
    component_rect = get_rectangle(component, target, legend)
    width, height = component_rect[2] - component_rect[0], component_rect[3] - component_rect[1]
    if not 0 < width <= 50 or not 0 < height <= 50:
        raise ValueError("A front elevation has empty bounds or exceeds 50 ft.")
    translate(document, component, legend, (cell[0] + cell[2] - width) / 2 - component_rect[0],
              note_rect[3] + clearance - component_rect[1])


def add_pair(document, source, target, legend, step):
    layout = legend["layout"]
    ids = List[DB.ElementId]()
    ids.Add(reader.positive_id(layout["seed"]["id"]))
    options = DB.CopyPasteOptions()
    try:
        copied = list(DB.ElementTransformUtils.CopyElements(source, ids, target, DB.Transform.Identity, options))
    finally:
        options.Dispose()
    if len(copied) != 1:
        raise ValueError("Revit did not copy exactly one legend component.")
    component = document.GetElement(copied[0])
    if (component is None or reader.element_id(component.OwnerViewId) != legend["id"]
            or reader.element_id(component.Category.Id) != str(int(DB.BuiltInCategory.OST_LegendComponents))):
        raise ValueError("The copied component is not owned by the target legend.")
    reference = component.get_Parameter(DB.BuiltInParameter.LEGEND_COMPONENT)
    direction = component.get_Parameter(DB.BuiltInParameter.LEGEND_COMPONENT_VIEW)
    if (reference is None or direction is None or reference.IsReadOnly or direction.IsReadOnly
            or (reader.element_id(reference.AsElementId()) != step["type_id"]
                and not reference.Set(reader.positive_id(step["type_id"])))):
        raise ValueError("Revit could not set the reviewed opening type on the copied component.")
    if direction.AsInteger() != -7 and not direction.Set(-7):
        raise ValueError("Revit could not set the verified front-elevation direction.")
    note = DB.TextNote.Create(document, target.Id, DB.XYZ(*legend["origin_ft"]), step["label"],
                              reader.positive_id(layout["text_type"]["id"]))
    return component, note


def refresh_pair(document, target, legend, step):
    component, note = element_pair(document, target, step)
    text_type = legend["layout"]["text_type"]["id"]
    if reader.element_id(note.GetTypeId()) != text_type:
        changed = note.ChangeTypeId(reader.positive_id(text_type))
        if reader.element_id(changed) not in ("-1", reader.element_id(note.Id)):
            raise ValueError("Changing the text type would replace a reviewed element.")
    if note.Text != step["label"]:
        note.Text = step["label"]
    return component, note


def pair_ids(component, note):
    return {"component_id": reader.element_id(component.Id), "note_id": reader.element_id(note.Id)}


def apply(document, plan):
    if document is None or document.IsReadOnly:
        raise ValueError("Open the reviewed writable host project.")
    request = snapshot_request(plan["basis"])
    current = reader.project_snapshot(document, request)
    checked = preview.approved_plan(plan, current)
    verify_ownership(document, checked["ownership_ids"])
    legend = current["legend"]
    target, source = [document.GetElement(reader.positive_id(request[key]))
                      for key in ("legend_id", "source_legend_id")]
    pairs = {row["type_uid"]: {"component_id": row["component"]["id"], "note_id": row["note"]["id"]}
             for row in (checked["intent"].get("receipt") or {}).get("pairs", [])}
    transaction = DB.Transaction(document, "Update door and window legend")
    if transaction.Start() != DB.TransactionStatus.Started:
        raise RuntimeError("Revit did not start the legend transaction.")
    try:
        for step in checked["steps"]:
            operation, uid = step["operation"], step["type_uid"]
            if operation == "retire":
                retire(document, target, step)
                del pairs[uid]
                continue
            if operation == "add":
                component, note = add_pair(document, source, target, legend, step)
            else:
                component, note = refresh_pair(document, target, legend, step)
            arrange(document, target, legend, component, note, step["cell_uv_ft"])
            pairs[uid] = pair_ids(component, note)
        document.Regenerate()
        result = preview.verify_result(checked, reader.project_snapshot(document, request), pairs)
        if transaction.Commit() != DB.TransactionStatus.Committed or transaction.GetStatus() != DB.TransactionStatus.Committed:
            raise RuntimeError("Revit did not confirm the legend transaction commit.")
    except Exception:
        if transaction.GetStatus() == DB.TransactionStatus.Started:
            transaction.RollBack()
        if transaction.GetStatus() != DB.TransactionStatus.RolledBack:
            raise RuntimeError("The legend transaction state is unresolved. Stop before another write.")
        raise
    after = preview.verify_result(checked, reader.project_snapshot(document, request), pairs)
    if result != after:
        raise ValueError("The legend changed during commit. Stop and check the runtime rollback.")
    return {"status": "ok", "receipt_json": json.dumps(after, ensure_ascii=True, allow_nan=False)}


if "doc" in globals() and "inputs" in globals():
    result = apply(doc, inputs["preview"])
