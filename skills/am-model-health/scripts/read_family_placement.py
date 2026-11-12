# -*- coding: utf-8 -*-
"""Read the saved project, reviewed level, and exact project family type in query mode."""
import hashlib
import json
from uuid import uuid4

MAX_FAMILIES = 5000
MAX_SYMBOLS = 250
MAX_ELEMENTS = 10000


def eid(element):
    return str(element.Id.Value)


def inventory(document, DB):
    elements = {}
    for collector in (DB.FilteredElementCollector(document).WhereElementIsElementType(),
                      DB.FilteredElementCollector(document).WhereElementIsNotElementType()):
        for element in collector:
            key = eid(element)
            if key in elements or len(elements) >= MAX_ELEMENTS:
                raise ValueError("The disposable project exceeds the complete inventory limit.")
            elements[key] = [str(element.UniqueId), str(element.VersionGuid)]
    return elements


def inventory_record(elements):
    encoded = json.dumps(elements, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(encoded.encode("ascii")).hexdigest(), "element_count": len(elements)}


def read_result(document, DB, request):
    snapshot = read_snapshot(document, DB, request)
    result = {"status": "ok", "snapshot_json": json.dumps(snapshot, ensure_ascii=True, allow_nan=False)}
    if request.get("audit_inventory") is True:
        import System
        absent = request.get("expect_absent_ids", [])
        if not isinstance(absent, list) or len(absent) > 3 or len(set(absent)) != len(absent):
            raise ValueError("Supply up to three distinct provisional element IDs.")
        elements = inventory(document, DB)
        for key in absent:
            number = int(key)
            if number <= 0 or str(number) != key:
                raise ValueError("Supply canonical positive provisional element ID strings.")
            if key in elements or document.GetElement(DB.ElementId(System.Int64(number))) is not None:
                raise ValueError("A provisional trial element still exists in the fresh read.")
        transaction = request.get("trial_transaction")
        if bool(absent) != bool(transaction) or (transaction and len(transaction) > 100):
            raise ValueError("Use the trial transaction name with its provisional IDs.")
        result["audit_token"] = uuid4().hex
        result["trial_transaction"] = transaction
        result["inventory"] = inventory_record(elements)
        result["absent_ids"] = sorted(absent, key=int)
    return result


def parameter_state(symbol, field):
    if field["shared_guid"]:
        from System import Guid
        parameter = symbol.get_Parameter(Guid(field["shared_guid"]))
        candidates = [parameter] if parameter is not None else []
    elif field["id"].startswith("-"):
        candidates = [p for p in symbol.Parameters if str(p.Id.Value) == field["id"]]
    else:
        candidates = list(symbol.GetParameters(field["name"]))
    if len(candidates) != 1:
        return {"state": "ambiguous" if candidates else "absent"}
    parameter = candidates[0]
    storage = str(parameter.StorageType)
    if storage != field["storage"] or str(parameter.Definition.GetDataType().TypeId) != field["data_type"]:
        return {"state": "different_definition"}
    if not parameter.HasValue:
        return {"state": "unset"}
    if storage == "String":
        value = parameter.AsString()
    elif storage == "Double":
        value = float(parameter.AsDouble())
    elif storage == "Integer":
        value = int(parameter.AsInteger())
    else:
        # Element IDs in a library family and project have different namespaces.
        return {"state": "not_comparable"}
    if storage == "String" and value is not None and len(value) > 4096:
        return {"state": "value_too_large"}
    return {"state": "value", "value": value}


def read_snapshot(document, DB, request, require_unmodified=True):
    if (document is None or not document.PathName or document.IsFamilyDocument or document.IsReadOnly
            or document.IsWorkshared or document.IsModelInCloud or (require_unmodified and document.IsModified)):
        raise ValueError("Open a saved, unmodified, local standalone Revit project.")
    selection = request["selection"]
    level_key = request["level_id"]
    import System
    level = document.GetElement(DB.ElementId(System.Int64(level_key)))
    if not isinstance(level, DB.Level) or eid(level) != level_key:
        raise ValueError("The reviewed project level is unavailable.")

    families = []
    exact = []
    for index, family in enumerate(DB.FilteredElementCollector(document).OfClass(DB.Family)):
        if index >= MAX_FAMILIES:
            raise ValueError("The family lookup exceeded its complete-scan limit.")
        if family.Name.lower() != selection["family"].lower():
            continue
        category = family.FamilyCategory
        family_row = {"id": eid(family), "uid": str(family.UniqueId), "name": family.Name,
                      "category_id": eid(category) if category is not None else None,
                      "placement_type": str(family.FamilyPlacementType)}
        families.append(family_row)
        if family_row["name"] != selection["family"] or family_row["category_id"] != selection["category"]["id"]:
            continue
        for index, identifier in enumerate(family.GetFamilySymbolIds()):
            if index >= MAX_SYMBOLS:
                raise ValueError("The type lookup exceeded its complete-scan limit.")
            symbol = document.GetElement(identifier)
            if not isinstance(symbol, DB.FamilySymbol) or eid(symbol.Family) != family_row["id"]:
                raise ValueError("A family type could not be read.")
            if symbol.Name == selection["type"]:
                exact.append({
                    "id": eid(symbol), "uid": str(symbol.UniqueId), "family_id": family_row["id"],
                    "family_uid": family_row["uid"], "active": bool(symbol.IsActive),
                    "placement_type": family_row["placement_type"],
                    "parameters": {field["id"]: parameter_state(symbol, field) for field in selection["parameters"]},
                })
    return {
        "kind": "family-placement-project-snapshot", "version": 1,
        "selection_sha256": selection["selection_sha256"],
        "project": {"project_uid": str(document.ProjectInformation.UniqueId),
                    "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()},
        "revit_year": str(document.Application.VersionNumber),
        "revit_build": str(document.Application.VersionBuild),
        "level": {"id": eid(level), "uid": str(level.UniqueId), "name": level.Name,
                  "project_elevation": float(level.ProjectElevation)},
        "same_name_families": sorted(families, key=lambda row: int(row["id"])),
        "exact_symbols": sorted(exact, key=lambda row: int(row["id"])),
    }


try:
    active_project, query_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = read_result(active_project, DB, query_inputs)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        result = {"status": "blocked", "reason": str(error)}
