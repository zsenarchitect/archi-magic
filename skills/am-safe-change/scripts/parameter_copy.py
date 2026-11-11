# -*- coding: utf-8 -*-
"""Read or execute parameter copies through the pyRevit Agent Runtime."""
import hashlib
import math
import time

from System import Double, Int32, Int64
from pyrevit.api import DB

import parameter_copy_preview as preview


def identifier(value):
    return DB.ElementId(Int64(int(preview.element_id(value))))


def project_identity():
    if doc is None or doc.IsFamilyDocument or not doc.PathName:  # noqa: F821
        raise ValueError("Open a saved Revit project first.")
    return {
        "uid": str(doc.ProjectInformation.UniqueId),  # noqa: F821
        "path_sha256": hashlib.sha256(doc.PathName.encode("utf-8")).hexdigest(),  # noqa: F821
    }


def parameter_for(element, name):
    matches = list(element.GetParameters(name))
    if len(matches) != 1:
        return None
    return matches[0]


def read_field(parameter, for_combination):
    storage = parameter.StorageType.ToString()
    data_type = parameter.Definition.GetDataType()
    state = {
        "identity": {
            "id": str(parameter.Id.Value),
            "guid": str(parameter.GUID) if parameter.IsShared else None,
        },
        "storage": storage,
        "spec": data_type.TypeId,
        "has_value": bool(parameter.HasValue),
        "read_only": bool(parameter.IsReadOnly),
        "global_controlled": parameter.GetAssociatedGlobalParameter() != DB.ElementId.InvalidElementId,
        "supports_reference": storage == "ElementId" and bool(data_type.TypeId) and DB.Parameter.IsSpec(data_type),
        "value": None,
    }
    if not state["has_value"]:
        return state
    if storage == "String":
        state["value"] = parameter.AsString()
    elif storage == "Integer":
        state["value"] = int(parameter.AsInteger())
        if for_combination:
            state["display"] = parameter.AsValueString() or str(state["value"])
    elif storage == "Double":
        value = float(parameter.AsDouble())
        if math.isnan(value) or math.isinf(value):
            state["error"] = "Revit returned a nonfinite number."
        else:
            state["value"] = value
            if for_combination:
                state["display"] = parameter.AsValueString()
    elif storage == "ElementId":
        ref_id = parameter.AsElementId()
        referent = doc.GetElement(ref_id) if ref_id.Value > 0 else None  # noqa: F821
        if referent is None:
            state["error"] = "Revit did not resolve a positive element reference."
        else:
            label = DB.Element.Name.GetValue(referent)
            state["value"] = {"id": str(ref_id.Value), "uid": str(referent.UniqueId), "label": label}
            if for_combination:
                state["display"] = label
    else:
        state["error"] = "This Revit storage type is unsupported."
    if isinstance(state["value"], preview.TEXT) and len(state["value"]) > preview.MAX_TEXT:
        raise ValueError("A parameter value exceeds 2,000 characters. Use a smaller scope.")
    return state


def read_snapshot(request):
    request = preview.normalize_request(request)
    identity = project_identity()
    category_id = request["category_id"]
    names = preview.field_names(request)
    combined = set(part["parameter"] for part in request.get("parts", []) if "parameter" in part)
    start = time.time()
    if "element_ids" in request:
        elements = (doc.GetElement(identifier(eid)) for eid in request["element_ids"])  # noqa: F821
    else:
        elements = (DB.FilteredElementCollector(doc)  # noqa: F821
                    .OfCategoryId(DB.ElementId(Int64(int(category_id)))).WhereElementIsNotElementType())
    rows = []
    size = 0
    for index, element in enumerate(elements, 1):
        if (index > preview.MAX_SCAN or len(rows) >= preview.MAX_ELEMENTS
                or time.time() - start > preview.SCAN_SECONDS):
            raise ValueError("Parameter read exceeded its budget. Select fewer instances.")
        if (element is None or isinstance(element, DB.ElementType) or element.Category is None
                or str(element.Category.Id.Value) != category_id):
            raise ValueError("An element is missing or does not belong to the requested instance category.")
        fields = {}
        for name in names:
            parameter = parameter_for(element, name)
            fields[name] = (read_field(parameter, name in combined) if parameter is not None
                            else {"error": "Parameter is missing or ambiguous."})
        scheme = element.DesignOption
        row = {"id": str(element.Id.Value), "uid": str(element.UniqueId), "fields": fields,
               "grouped": element.GroupId.Value > 0,
               "scheme": str(scheme.Id.Value) if scheme is not None else "primary",
               "category": element.Category.Name}
        size += len(preview.canonical(row))
        if size > preview.MAX_SNAPSHOT:
            raise ValueError("The parameter snapshot exceeds 512 KiB. Select fewer instances.")
        rows.append(row)
    rows.sort(key=lambda row: int(row["id"]))
    snapshot = {"request": request, "project": identity, "rows": rows}
    if not rows or len(preview.canonical(snapshot)) > preview.MAX_SNAPSHOT:
        raise ValueError("The category is empty or exceeds the snapshot budget.")
    return snapshot


def check_ownership(eid):
    if not doc.IsWorkshared:  # noqa: F821
        return
    checkout = str(DB.WorksharingUtils.GetCheckoutStatus(doc, eid))  # noqa: F821
    updates = str(DB.WorksharingUtils.GetModelUpdatesStatus(doc, eid))  # noqa: F821
    if checkout not in ("OwnedByCurrentUser", "NotOwned") or updates not in ("CurrentWithCentral", "NotYetInCentral"):
        raise ValueError("Target ownership or central model state prevents the change.")


def set_value(parameter, step):
    storage, value = step["storage"], step["value"]
    if storage == "Integer":
        return parameter.Set(Int32(value))
    if storage == "Double":
        return parameter.Set(Double(value))
    if storage == "String":
        return parameter.Set(value)
    return parameter.Set(identifier(value["id"]))


def apply(args):
    approved = args["plan"]
    shown = args["preview"]
    if doc.IsReadOnly:  # noqa: F821
        raise ValueError("The active model is read-only.")
    if shown.get("blocked") is not False or any(warning.get("blocking") for warning in shown.get("warnings", [])):
        raise ValueError("The reviewed preview contains blocking warnings.")
    current = read_snapshot(approved["request"])
    fresh = preview.compute(current)
    checked_fields = ("kind", "keyColumn", "columns", "before", "after", "ownership_ids", "parameter_copy_basis")
    if (approved != fresh["plan"] or fresh["preview"]["blocked"] or not approved["steps"]
            or any(shown.get(field) != fresh["preview"][field] for field in checked_fields)):
        raise ValueError("The project, approved preview or parameter values changed. Read and preview again.")
    name = approved["request"]["destination"]
    targets = []
    for step in approved["steps"]:
        eid = identifier(step["id"])
        check_ownership(eid)
        element = doc.GetElement(eid)  # noqa: F821
        parameter = parameter_for(element, name) if element is not None else None
        if parameter is None:
            raise ValueError("The destination parameter is unavailable.")
        targets.append((step, parameter))

    txn = DB.Transaction(doc, "Copy or combine instance parameters")  # noqa: F821
    if txn.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "The Revit transaction did not start."}
    try:
        for step, parameter in targets:
            if not set_value(parameter, step):
                raise ValueError("Revit declined a parameter write.")
        doc.Regenerate()  # noqa: F821
        for step, parameter in targets:
            after = read_field(parameter, False)
            if after["value"] != step["value"]:
                raise ValueError("A parameter readback differs from the approved value.")
        status = txn.Commit()
        if status == DB.TransactionStatus.Committed:
            return {"status": "ok", "rows_json": preview.canonical(approved["steps"])}
        if status == DB.TransactionStatus.RolledBack:
            return {"status": "stopped", "message": "Revit rolled back the parameter change."}
        return {"status": "transaction_unresolved", "message": "Check the Revit transaction before continuing."}
    except Exception as error:
        # A failed regeneration can invalidate the document. Read only transaction status.
        try:
            status = txn.GetStatus()
            if status == DB.TransactionStatus.Started:
                status = txn.RollBack()
            if status == DB.TransactionStatus.RolledBack:
                message = error.args[0] if isinstance(error, ValueError) else "Revit failed during the parameter change."
                return {"status": "stopped", "message": message}
        except Exception:
            return {"status": "transaction_unresolved", "message": "Could not verify parameter rollback."}
        return {"status": "transaction_unresolved", "message": "Check the Revit transaction before continuing."}


def main(args):
    mode = args.get("mode", "read")
    if mode == "read":
        return {"status": "ok", "snapshot_json": preview.canonical(read_snapshot(args["request"]))}
    if mode == "apply":
        return apply(args)
    raise ValueError("Choose read or apply.")


try:
    result = main(inputs)  # noqa: F821
except (KeyError, TypeError, ValueError) as error:
    result = {"status": "stopped", "message": error.args[0]}
except Exception:
    result = {"status": "stopped", "message": "Revit could not read or change the selected parameters."}
