# -*- coding: utf-8 -*-
"""Read the used host wall and floor types for the compound legend."""
import hashlib

from pyrevit.api import DB
from System import Int64
from System.Diagnostics import Stopwatch

import compound_legend_preview as preview


def identifier(value):
    return DB.ElementId(Int64(int(preview.positive_id(value))))


def key(element_id):
    return str(element_id.Value)


def element_name(element):
    return DB.Element.Name.GetValue(element)


def read_layers(document, structure, remaining):
    result = []
    for index, layer in enumerate(structure.GetLayers()):
        if index >= remaining:
            raise ValueError("The scope exceeds 1,000 layer entries. Select a smaller batch.")
        material_id = key(layer.MaterialId)
        material = document.GetElement(layer.MaterialId) if int(material_id) > 0 else None
        material_name = ("By Category" if material_id == "-1"
                         else element_name(material) if isinstance(material, DB.Material) else None)
        result.append({
            "material_id": material_id, "material": material_name,
            "material_uid": str(material.UniqueId) if isinstance(material, DB.Material) else None,
            "function": str(layer.Function), "width_ft": float(layer.Width),
            "variable": index == structure.VariableLayerIndex,
        })
    return result


def target_settings(document, request, clock):
    if "view_name" not in request:
        return None
    draft = document.GetElement(identifier(request["drafting_type_id"]))
    text = document.GetElement(identifier(request["text_type_id"]))
    if not isinstance(draft, DB.ViewFamilyType) or draft.ViewFamily != DB.ViewFamily.Drafting:
        raise ValueError("Choose a drafting view family type.")
    if not isinstance(text, DB.TextNoteType):
        raise ValueError("Choose a text note type.")
    name = request["view_name"]
    for index, view in enumerate(DB.FilteredElementCollector(document).OfClass(DB.View), 1):
        if index > preview.MAX_SCAN or clock.ElapsedMilliseconds >= 10000:
            raise ValueError("Could not check the requested view name within the scan budget.")
        if element_name(view).lower() == name.lower():
            raise ValueError("A view already uses the requested name.")
    return {"name": name, "drafting_type_uid": str(draft.UniqueId), "text_type_uid": str(text.UniqueId)}


def collect(document, request, clock):
    classes = ((DB.Wall, "Wall"), (DB.Floor, "Floor"))
    if "element_ids" in request:
        elements = (document.GetElement(identifier(item)) for item in request["element_ids"])
    else:
        elements = (element for cls, name in classes if name == request.get("category", name)
                    for element in DB.FilteredElementCollector(document).OfClass(cls).WhereElementIsNotElementType())
    found = {}
    for index, element in enumerate(elements, 1):
        if index > preview.MAX_SCAN or clock.ElapsedMilliseconds >= 10000:
            raise ValueError("The wall and floor scan exceeded its budget. Select a smaller scope.")
        category = next((name for cls, name in classes if isinstance(element, cls)), None)
        if category is None or category != request.get("category", category):
            raise ValueError("A selected element is unavailable or outside the wall/floor scope.")
        type_id = element.GetTypeId()
        item = found.setdefault(key(type_id), [type_id, category, 0])
        if item[1] != category:
            raise ValueError("A type ID is shared by incompatible instance categories.")
        item[2] += 1
    return found


def read_snapshot(document, request):
    if document is None or document.IsFamilyDocument or not document.PathName:
        raise ValueError("Open a saved Revit project.")
    request = preview.read_request(request)
    clock = Stopwatch.StartNew()
    target = target_settings(document, request, clock)
    used = collect(document, request, clock)
    types, excluded = [], []
    remaining = preview.MAX_LAYERS
    for type_id, category, instances in sorted(used.values(), key=lambda item: int(key(item[0]))):
        if clock.ElapsedMilliseconds >= 10000:
            raise ValueError("The compound type read exceeded its time budget.")
        host_type = document.GetElement(type_id)
        expected = DB.WallType if category == "Wall" else DB.FloorType
        if not isinstance(host_type, expected):
            raise ValueError("An in-use wall or floor type could not be resolved.")
        row = {"id": key(type_id), "uid": str(host_type.UniqueId), "category": category,
               "name": element_name(host_type), "instances": instances}
        structure = host_type.GetCompoundStructure()
        if structure is None or not structure.LayerCount:
            excluded.append(row)
            continue
        if len(types) >= preview.MAX_TYPES:
            raise ValueError("The scope exceeds 100 compound types. Select a smaller batch.")
        if category == "Wall":
            nominal = float(host_type.Width)
        else:
            parameter = host_type.get_Parameter(DB.BuiltInParameter.FLOOR_ATTR_DEFAULT_THICKNESS_PARAM)
            nominal = (float(parameter.AsDouble()) if parameter is not None and parameter.HasValue
                       and parameter.StorageType == DB.StorageType.Double else None)
        layers = read_layers(document, structure, remaining)
        remaining -= len(layers)
        row.update(width_ft=nominal, layers=layers)
        if structure.IsVerticallyCompound:
            height = float(structure.SampleHeight)
            if height <= 0:
                raise ValueError("A vertically compound type has no valid sample height.")
            simple = structure.GetSimpleCompoundStructure(height, height / 2.0)
            layers = read_layers(document, simple, remaining)
            remaining -= len(layers)
            row.update(grid_width_ft=float(structure.GetWidth()),
                       slice={"height_ft": height / 2.0, "width_ft": float(simple.GetWidth()), "layers": layers})
        types.append(row)
    if clock.ElapsedMilliseconds >= 10000:
        raise ValueError("The compound type read exceeded its time budget.")
    return {"kind": "compound-type-snapshot", "request": request, "target": target,
            "project": {"uid": str(document.ProjectInformation.UniqueId),
                        "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()},
            "types": types, "excluded": excluded}


if "inputs" in globals():
    try:
        snapshot = preview.canonical(read_snapshot(doc, inputs.get("request", {})))  # noqa: F821
        if len(snapshot) > preview.MAX_BYTES:
            raise ValueError("The compound type snapshot exceeds 1 MiB. Select a smaller scope.")
        result = {"status": "ok", "snapshot_json": snapshot}
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": error.args[0]}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not read the requested compound type data.",
                  "error_type": type(error).__name__}
