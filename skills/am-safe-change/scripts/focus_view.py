# -*- coding: utf-8 -*-
"""Read, apply, or verify one focus view through the pyRevit Agent Runtime."""
import hashlib
import itertools
import json
import re

from System import Int64
from pyrevit.api import DB

import focus_view_preview as preview


def identifier(value):
    if not re.match(r"^[1-9][0-9]{0,18}\Z", str(value)) or int(value) > 2**63 - 1:
        raise ValueError("Use a positive decimal Revit ID.")
    return DB.ElementId(Int64(int(value)))


def get_id(element):
    return str(element.Id.Value)


def project_identity():
    if doc is None or doc.IsFamilyDocument or doc.IsReadOnly or not doc.PathName:  # noqa: F821
        raise ValueError("Open the saved, writable project first.")
    return {"project_uid": str(doc.ProjectInformation.UniqueId),  # noqa: F821
            "path_sha256": hashlib.sha256(doc.PathName.encode("utf-8")).hexdigest()}  # noqa: F821


def coords(point):
    return preview.triple([float(point.X), float(point.Y), float(point.Z)])


def describe_box(box):
    if box is None:
        raise ValueError("The view has no supported box.")
    transform = box.Transform
    frame = [coords(axis) for axis in (transform.Origin, transform.BasisX, transform.BasisY, transform.BasisZ)]
    preview.check_frame(frame)
    return {"frame": frame, "min": coords(box.Min), "max": coords(box.Max)}


def current_selection():
    ids = list(__revit__.ActiveUIDocument.Selection.GetElementIds())  # noqa: F821
    if not 1 <= len(ids) <= 100:
        raise ValueError("Select one to 100 host model elements.")
    return sorted(ids, key=lambda x: x.Value)


def read_selection(ids):
    rows = []
    for element_id in ids:
        element = doc.GetElement(element_id)  # noqa: F821
        if (element is None or element.ViewSpecific or isinstance(element, (DB.RevitLinkInstance, DB.ElementType))):
            raise ValueError("The selection must contain supported host model elements.")
        box = element.get_BoundingBox(None)
        if box is None:
            raise ValueError("A selected element has no model box.")
        low, high = coords(box.Min), coords(box.Max)
        if not all(a <= b for a, b in zip(low, high)) or low == high:
            raise ValueError("A selected element has invalid model bounds.")
        points = [coords(box.Transform.OfPoint(DB.XYZ(*point)))
                  for point in itertools.product(*zip(low, high))]
        rows.append({"id": get_id(element), "uid": str(element.UniqueId), "corners": points})
    return rows


def plan_range(view):
    planes = (DB.PlanViewPlane.TopClipPlane, DB.PlanViewPlane.CutPlane,
              DB.PlanViewPlane.BottomClipPlane, DB.PlanViewPlane.ViewDepthPlane)
    view_range = view.GetViewRange()
    return [[str(view_range.GetLevelId(plane).Value), float(view_range.GetOffset(plane))]
            for plane in planes]


def target_box(view, action):
    if action == "crop_plan":
        return describe_box(view.CropBox), bool(view.CropBoxActive), plan_range(view)
    return describe_box(view.GetSectionBox()), bool(view.IsSectionBoxActive), None


def check_view(view, action):
    kind = DB.ViewPlan if action == "crop_plan" else DB.View3D
    if not isinstance(view, kind) or view.IsTemplate or view.ViewTemplateId.Value != -1:
        raise ValueError("Use a supported view without a template.")
    if view.GetPrimaryViewId().Value != -1 or len(list(view.GetDependentViewIds())):
        raise ValueError("Use an independent view without dependent views.")
    if action != "crop_plan":
        if view.IsPerspective or view.IsLocked:
            raise ValueError("Use an unlocked orthographic 3D view.")
        return
    scope = view.get_Parameter(DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
    if scope is not None and (scope.IsReadOnly or scope.AsElementId().Value != -1):
        raise ValueError("Use a plan without a scope box or read-only crop.")
    manager = view.GetCropRegionShapeManager()
    if manager is None or manager.ShapeSet or manager.Split:
        raise ValueError("Use a plan with an unsplit rectangular crop.")


def owner_state(view):
    if not doc.IsWorkshared:  # noqa: F821
        return None
    status = str(DB.WorksharingUtils.GetCheckoutStatus(doc, view.Id))  # noqa: F821
    updates = str(DB.WorksharingUtils.GetModelUpdatesStatus(doc, view.Id))  # noqa: F821
    if status not in ("OwnedByCurrentUser", "NotOwned") or updates not in ("CurrentWithCentral", "NotYetInCentral"):
        raise ValueError("The target view ownership or central status blocks the change.")
    return [status, updates]


def read_target(request):
    action = request["action"]
    selected = doc.GetElement(identifier(request["target_id"]))  # noqa: F821
    if action == "create_3d":
        if (not isinstance(selected, DB.ViewFamilyType) or selected.ViewFamily != DB.ViewFamily.ThreeDimensional
                or selected.DefaultTemplateId.Value != -1):
            raise ValueError("Choose a three-dimensional view type without a default template.")
        name = request["name"]
        if (not isinstance(name, (type(u""), str)) or not name.strip() or name != name.strip()
                or len(name) > 100 or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValueError("Use a new view name with one to 100 display characters.")
        if any(view.Name.upper() == name.upper() for view in DB.FilteredElementCollector(doc).OfClass(DB.View)):  # noqa: F821
            raise ValueError("The requested view name already exists.")
        return {"id": get_id(selected), "uid": str(selected.UniqueId), "name": name, "placements": []}
    check_view(selected, action)
    placement = str(selected.GetPlacementOnSheetStatus())
    placements = []
    if placement != "NotPlaced":
        if request["allow_placed"] is not True:
            raise ValueError("Review and explicitly authorize the placed view scope.")
        placements = sorted([[get_id(v), str(v.SheetId.Value)]
                             for v in DB.FilteredElementCollector(doc).OfClass(DB.Viewport)  # noqa: F821
                             if v.ViewId.Value == selected.Id.Value])
        if not placements:
            raise ValueError("The view's sheet placement could not be verified.")
    box, active, view_range = target_box(selected, action)
    return {"id": get_id(selected), "uid": str(selected.UniqueId), "name": selected.Name,
            "box": box, "box_active": active, "view_range": view_range,
            "placements": placements, "ownership": owner_state(selected)}


def read_snapshot(raw):
    request = {"action": raw["action"], "target_id": str(identifier(raw["target_id"]).Value),
               "padding_ft": preview.real(raw["padding_ft"], 0.01, 100)}
    if request["action"] not in preview.ACTIONS:
        raise ValueError("Choose create_3d, reuse_3d, or crop_plan.")
    if request["action"] == "create_3d":
        request["name"] = raw["name"]
    else:
        request["allow_placed"] = raw.get("allow_placed") is True
    identity = project_identity()
    selection = read_selection(current_selection())
    return {"kind": "focus-view-snapshot", "document": identity, "request": request,
            "active_view_uid": str(doc.ActiveView.UniqueId),  # noqa: F821
            "selection": selection, "target": read_target(request)}


def inspect_view(view, action):
    check_view(view, action)
    box, active, view_range = target_box(view, action)
    ids = current_selection()
    return {"status": "ok", "document": project_identity(), "view_id": get_id(view),
            "view_uid": str(view.UniqueId), "name_json": json.dumps(view.Name, ensure_ascii=True),
            "category_json": json.dumps(view.Category.Name if view.Category is not None else "(no category)",
                                        ensure_ascii=True),
            "box": box, "box_active": active, "view_range": view_range,
            "active_view_uid": str(doc.ActiveView.UniqueId),  # noqa: F821
            "selection_ids": [str(i.Value) for i in ids],
            "selection_json": json.dumps(read_selection(ids), ensure_ascii=True, allow_nan=False,
                                         separators=(",", ":"))}


def apply(plan):
    if (plan.get("kind") != "change-preview" or plan.get("recipe") != "focus-view"
            or plan.get("blocked") is not False or any(w.get("blocking") for w in plan.get("warnings", []))):
        raise ValueError("Supply the approved, unblocked focus view preview.")
    basis = plan["basis"]
    actual = read_snapshot(basis["request"])
    computed = preview.compute(actual)
    if actual != basis or computed["box"] != plan["box"]:
        raise ValueError("The selection, view, ownership, or geometry changed after preview.")
    action = basis["request"]["action"]
    txn = DB.Transaction(doc, "Focus Revit view")  # noqa: F821
    if txn.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the view transaction."}
    try:
        target_id = identifier(basis["target"]["id"])
        if action == "create_3d":
            view = DB.View3D.CreateIsometric(doc, target_id)  # noqa: F821
            view.Name = basis["request"]["name"]
        else:
            view = doc.GetElement(target_id)  # noqa: F821
        check_view(view, action)
        box = view.CropBox if action == "crop_plan" else DB.BoundingBoxXYZ()
        box.Min = DB.XYZ(*computed["box"]["min"])
        box.Max = DB.XYZ(*computed["box"]["max"])
        if action == "crop_plan":
            view.CropBox = box
            view.CropBoxActive = True
        else:
            box.Transform = DB.Transform.Identity
            view.SetSectionBox(box)
            view.IsSectionBoxActive = True
        doc.Regenerate()  # noqa: F821
        readback = inspect_view(view, action)
        preview.check_result(plan, readback)
        state = txn.Commit()
        if state != DB.TransactionStatus.Committed:
            return abort_transaction(txn, state)
        return readback
    except Exception:
        return abort_transaction(txn)


def abort_transaction(txn, state=None):
    try:
        if state is None:
            state = txn.GetStatus()
        if state == DB.TransactionStatus.Started:
            state = txn.RollBack()
        if state == DB.TransactionStatus.RolledBack:
            return {"status": "stopped", "message": "The view change failed verification and was rolled back."}
    except Exception:
        return {"status": "transaction_unresolved", "message": "The Revit transaction state could not be read."}
    return {"status": "transaction_unresolved", "message": "Check the Revit transaction state before continuing."}


def main(args):
    mode = args.get("mode", "read")
    if mode == "read":
        snapshot = read_snapshot(args["request"])
        return {"status": "ok", "snapshot_json": json.dumps(snapshot, ensure_ascii=True, allow_nan=False,
                                                               sort_keys=True, separators=(",", ":"))}
    if mode == "apply":
        return apply(args["plan"])
    if mode == "verify":
        view = doc.GetElement(identifier(args["view_id"]))  # noqa: F821
        if view is None or str(view.UniqueId) != args["view_uid"] or args["action"] not in preview.ACTIONS:
            raise ValueError("The exact kept view is unavailable.")
        return inspect_view(view, args["action"])
    raise ValueError("Choose read, apply, or verify.")


try:
    result = main(inputs)  # noqa: F821
except (KeyError, TypeError, ValueError) as error:
    result = {"status": "stopped", "message": str(error)}
except Exception:
    result = {"status": "stopped", "message": "Revit could not read the requested view state."}
