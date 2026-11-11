# -*- coding: utf-8 -*-
"""Inspect or select one directly linked Revit element in the active host view."""
import hashlib
import json
import math
import re

from System import Int64
from System.Collections.Generic import List
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

try:
    TEXT = basestring
    unicode_text = unicode
except NameError:
    TEXT = str
    unicode_text = str

MAX_SCAN = 5000
MAX_REFERENCES = 500
MAX_PARAMETERS = 40
MAX_NAME = 256
MAX_TEXT = 2000
MAX_RESULT = 128 * 1024


def positive_id(value):
    if (not isinstance(value, TEXT) or not re.match(r"[1-9][0-9]{0,18}\Z", value)
            or int(value) >= 2 ** 63):
        raise ValueError("Use positive decimal element IDs as strings.")
    return DB.ElementId(Int64(int(value)))


def key(element_id):
    return str(element_id.Value)


def name_of(element):
    return DB.Element.Name.GetValue(element)


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def bounded_text(value, limit=MAX_TEXT):
    if value is None:
        return None, False
    value = unicode_text(value)
    return value[:limit], len(value) > limit


def model_identity(document):
    try:
        if document.IsModelInCloud:
            path = document.GetCloudModelPath()
            project = str(path.GetProjectGUID())
            model = str(document.CloudModelGUID)
            region = str(path.Region)
            empty = "00000000-0000-0000-0000-000000000000"
            if not project or not model or project == empty or model == empty or not region:
                return None
            return {"kind": "cloud", "sha256": hashlib.sha256((region + "/" + project + "/" + model).encode("utf-8")).hexdigest()}
        path = document.PathName
        info = document.ProjectInformation
        if path and info is not None and info.UniqueId:
            return {"kind": "saved", "uid": str(info.UniqueId),
                    "path_sha256": hashlib.sha256(path.encode("utf-8")).hexdigest()}
    except Exception:
        return None
    return None


def active_view(host, ui):
    if host is None or host.IsFamilyDocument or ui is None or not ui.Document.Equals(host):
        raise ValueError("Activate the host Revit project and view.")
    view = ui.ActiveView
    if view is None or view.IsTemplate:
        raise ValueError("Activate a model view in the host project.")
    return view


def linked_document(host, link_id):
    link = host.GetElement(positive_id(link_id))
    if not isinstance(link, DB.RevitLinkInstance):
        raise ValueError("Choose a Revit link instance in the host project.")
    link_type = host.GetElement(link.GetTypeId())
    if not isinstance(link_type, DB.RevitLinkType) or link_type.IsNestedLink:
        raise ValueError("Choose a directly linked RVT instance.")
    linked = link.GetLinkDocument()
    if linked is None or linked.IsFamilyDocument:
        raise ValueError("The selected Revit link is unloaded or unavailable.")
    return link, linked


def linked_element(linked, element_id):
    element = linked.GetElement(positive_id(element_id))
    if element is None or isinstance(element, (DB.ElementType, DB.RevitLinkInstance)):
        raise ValueError("Choose a linked model instance element in the selected RVT.")
    return element


def reference_pair(reference):
    return {"link_instance_id": key(reference.ElementId), "linked_element_id": key(reference.LinkedElementId)}


def references(ui):
    result = []
    for reference in ui.Selection.GetReferences():
        if len(result) >= MAX_REFERENCES:
            raise ValueError("Select at most 500 references before running the inspector.")
        result.append(reference)
    return result


def stable_refs(host, items):
    values = [reference.ConvertToStableRepresentation(host) for reference in items]
    if any(not value for value in values):
        raise ValueError("Revit did not return an exact selection identity.")
    return sorted(values)


def target_for(host, view, link, linked, element):
    host_identity, linked_identity = model_identity(host), model_identity(linked)
    if not host_identity or not linked_identity:
        return None
    return {
        "host": host_identity, "linked_document": linked_identity,
        "view_id": key(view.Id), "view_uid": str(view.UniqueId),
        "link_instance_id": key(link.Id), "link_instance_uid": str(link.UniqueId),
        "linked_element_id": key(element.Id), "linked_element_uid": str(element.UniqueId),
    }


def exact_name(linked, category, name):
    if (not isinstance(category, TEXT) or not re.match(r"-[1-9][0-9]{0,9}\Z", category)
            or int(category) < -(2 ** 31)):
        raise ValueError("Supply a built-in linked category ID as a negative decimal string.")
    if not isinstance(name, TEXT) or not name.strip() or len(name) > MAX_NAME:
        raise ValueError("Supply an exact element name of up to 256 characters.")
    clock = Stopwatch.StartNew()
    collector = DB.FilteredElementCollector(linked).OfCategoryId(DB.ElementId(Int64(int(category))))
    count, candidates, match = 0, [], None
    for index, element in enumerate(collector.WhereElementIsNotElementType(), 1):
        if index > MAX_SCAN or clock.ElapsedMilliseconds >= 8000:
            return None, {"status": "incomplete", "reason": "Name search exceeded its budget. Use a linked element ID."}
        try:
            same_name = name_of(element) == name
        except Exception:
            return None, {"status": "incomplete", "reason": "An element name could not be read. Use a linked element ID."}
        if same_name and not isinstance(element, DB.RevitLinkInstance):
            count += 1
            if len(candidates) < 20:
                candidates.append(key(element.Id))
            match = element
    if clock.ElapsedMilliseconds >= 8000:
        return None, {"status": "incomplete", "reason": "Name search exceeded its budget. Use a linked element ID."}
    if count == 1:
        return match, None
    if count == 0:
        return None, {"status": "not_found", "reason": "No exact element name in the selected link and category."}
    return None, {"status": "ambiguous", "matches": count, "candidate_ids": sorted(candidates, key=int),
                  "candidates_omitted": count - len(candidates)}


def parameter_names(raw):
    if raw is None:
        return None
    if (not isinstance(raw, list) or not 1 <= len(raw) <= 20
            or any(not isinstance(name, TEXT) or not name.strip() or len(name) > MAX_NAME for name in raw)
            or len(set(raw)) != len(raw)):
        raise ValueError("Supply 1 to 20 unique exact parameter names.")
    return raw


def read_parameter(parameter):
    storage = str(parameter.StorageType)
    label, label_cut = bounded_text(parameter.Definition.Name)
    row = {"id": key(parameter.Id), "name": label, "storage": storage, "has_value": bool(parameter.HasValue),
           "value": None}
    if label_cut:
        row["name_truncated"] = True
    if not row["has_value"]:
        row["state"] = "unset"
        return row
    if storage == "String":
        row["value"], cut = bounded_text(parameter.AsString())
        if cut:
            row["value_truncated"] = True
    elif storage == "Integer":
        row["value"] = int(parameter.AsInteger())
    elif storage == "Double":
        value = float(parameter.AsDouble())
        if math.isnan(value) or math.isinf(value):
            row["state"] = "unsupported_value"
            return row
        row["value"] = value
        row["units"] = "revit_internal"
    elif storage == "ElementId":
        row["value"] = key(parameter.AsElementId())
    else:
        row["state"] = "unsupported_storage"
        return row
    row["state"] = "value"
    if storage in ("Integer", "Double", "ElementId"):
        try:
            formatted, cut = bounded_text(parameter.AsValueString())
            if formatted is not None:
                row["display"] = formatted
            if cut:
                row["display_truncated"] = True
        except Exception:
            row["display_unavailable"] = True
    return row


def read_parameters(element, names):
    missing, rows, limited = [], [], False
    if names is None:
        source = [(None, element.GetOrderedParameters())]
    else:
        source = ((name, element.GetParameters(name)) for name in names)
    for name, parameters in source:
        if name is not None and not parameters.Count:
            missing.append(name)
        for parameter in parameters:
            if len(rows) == MAX_PARAMETERS:
                limited = True
                break
            try:
                rows.append(read_parameter(parameter))
            except Exception as error:
                rows.append({"id": key(parameter.Id), "state": "read_failed", "error_type": type(error).__name__})
        if limited:
            break
    return rows, missing, limited


def inspect(host, ui, view, args):
    names = parameter_names(args.get("parameter_names"))
    if not any(field in args for field in ("link_instance_id", "linked_element_id", "category_id", "name")):
        picked = references(ui)
        if len(picked) != 1 or int(key(picked[0].LinkedElementId)) <= 0:
            raise ValueError("Use Tab in the host view to select one linked element, then inspect again.")
        pair = reference_pair(picked[0])
        link, linked = linked_document(host, pair["link_instance_id"])
        element = linked_element(linked, pair["linked_element_id"])
    else:
        link, linked = linked_document(host, args["link_instance_id"])
        if "linked_element_id" in args:
            if "name" in args or "category_id" in args:
                raise ValueError("Choose a linked ID or a category and exact name.")
            element = linked_element(linked, args["linked_element_id"])
        else:
            element, failure = exact_name(linked, args.get("category_id"), args.get("name"))
            if failure:
                failure["link_instance_id"] = key(link.Id)
                return failure

    params, missing, limited = read_parameters(element, names)
    target = target_for(host, view, link, linked, element)
    category = element.Category
    result = {
        "status": "inspected", "link_instance_id": key(link.Id), "link_instance_uid": str(link.UniqueId),
        "link_name": bounded_text(name_of(link), MAX_NAME)[0], "linked_document_title": bounded_text(linked.Title, MAX_NAME)[0],
        "linked_element_id": key(element.Id), "linked_element_uid": str(element.UniqueId),
        "element_name": bounded_text(name_of(element), MAX_NAME)[0],
        "category": bounded_text(category.Name, MAX_NAME)[0] if category is not None else None,
        "category_id": key(category.Id) if category is not None else None,
        "parameter_scope": "visible" if names is None else "named", "parameters": params,
        "missing_parameter_names": missing, "parameters_limited": limited, "selection_target": target,
    }
    if target is None:
        result["selection_unavailable"] = "Revit did not expose an exact identity for both models."
    while len(encode(result)) > MAX_RESULT and params:
        params.pop()
        result["parameters_limited"] = True
    return result


def typed_refs(items):
    values = List[DB.Reference]()
    for item in items:
        values.Add(item)
    return values


def select(host, ui, view, target):
    if not isinstance(target, dict):
        raise ValueError("Supply the target returned by a fresh inspection.")
    link, linked = linked_document(host, target["link_instance_id"])
    element = linked_element(linked, target["linked_element_id"])
    fresh = target_for(host, view, link, linked, element)
    if fresh is None or fresh != target:
        raise ValueError("The host view, link, or linked element changed. Inspect it again.")
    created = DB.Reference(element).CreateLinkReference(link)
    expected = {"link_instance_id": target["link_instance_id"], "linked_element_id": target["linked_element_id"]}
    if reference_pair(created) != expected:
        raise ValueError("Revit could not construct the exact linked reference.")
    before = references(ui)
    prior_identity = stable_refs(host, before)
    desired, restore = typed_refs([created]), typed_refs(before)
    failure = {"status": "selection_failed", "reason": "reference_readback_mismatch"}
    try:
        ui.Selection.SetReferences(desired)
        if [reference_pair(item) for item in references(ui)] == [expected]:
            return dict(expected, status="selected", view_id=key(view.Id))
    except Exception as error:
        failure.update(reason="selection_operation_failed", error_type=type(error).__name__)
    try:
        ui.Selection.SetReferences(restore)
    except Exception as error:
        # The setter can throw after changing the selection. Read it before reporting restoration.
        failure["restore_error_type"] = type(error).__name__
    try:
        restored = stable_refs(host, references(ui)) == prior_identity
    except Exception as error:
        restored = False
        failure["restore_read_error_type"] = type(error).__name__
    failure["previous_selection"] = "restored" if restored else "unverified"
    return failure


def main(host, ui, args):
    view = active_view(host, ui)
    mode = args.get("mode", "inspect")
    fields = {
        "inspect": {"mode", "link_instance_id", "linked_element_id", "category_id", "name", "parameter_names"},
        "select": {"mode", "target"},
        "selection": {"mode"},
    }
    if mode not in fields or set(args) - fields[mode]:
        raise ValueError("Check the inspector mode and input fields.")
    if mode == "inspect":
        return inspect(host, ui, view, args)
    if mode == "select":
        return select(host, ui, view, args.get("target"))
    if mode == "selection":
        return {"status": "selection_read", "view_id": key(view.Id), "view_uid": str(view.UniqueId),
                "references": [reference_pair(item) for item in references(ui)]}
    raise ValueError("Choose inspect, select, or selection.")


if "inputs" in globals():
    try:
        report = main(doc, uidoc, inputs)  # noqa: F821
        payload = encode(report)
        if len(payload) > MAX_RESULT:
            raise ValueError("The inspector response exceeds 128 KiB.")
        result = {"status": report["status"], "payload": payload}
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": error.args[0]}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not complete the linked-element operation.",
                  "error_type": type(error).__name__}
