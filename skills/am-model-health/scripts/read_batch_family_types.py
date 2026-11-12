"""Read exact existing family types and validate batch size units in Revit query mode."""

import json
import math
from uuid import uuid4

from batch_family_type_preview import validate_record
from read_family_placement import MAX_FAMILIES, MAX_SYMBOLS, eid, inventory, inventory_record, parameter_state, saved_project_identity
from batch_family_type_table import MAX_FAMILIES as MAX_BATCH_FAMILIES, MAX_ROWS


def internal_size(DB, size):
    field = size["definition"]
    spec = DB.ForgeTypeId(field["data_type"])
    number = float(size["value"])
    if field["storage"] != "Double" or not math.isfinite(number):
        raise ValueError("Expected a finite Double value.")
    if size["unit"] == "1":
        if not spec.Equals(DB.SpecTypeId.Number):
            raise ValueError("The source parameter is not dimensionless.")
        return number
    unit = getattr(DB.UnitTypeId, {
        "mm": "Millimeters", "cm": "Centimeters", "m": "Meters", "in": "Inches", "ft": "Feet",
    }[size["unit"]])
    if not spec.Equals(DB.SpecTypeId.Length) or not DB.UnitUtils.IsValidUnit(spec, unit):
        raise ValueError("The size unit is incompatible with the source parameter spec.")
    value = float(DB.UnitUtils.ConvertToInternalUnits(number, unit))
    if not math.isfinite(value):
        raise ValueError("The Revit internal value is not finite.")
    return value


def matching_families(document, DB, names):
    result = {}
    for index, family in enumerate(DB.FilteredElementCollector(document).OfClass(DB.Family)):
        if index >= MAX_FAMILIES:
            raise ValueError("The project family read exceeded its complete-scan limit.")
        key = str(family.Name).casefold()
        if key not in names:
            continue
        symbols = []
        for position, identifier in enumerate(family.GetFamilySymbolIds()):
            if position >= MAX_SYMBOLS:
                raise ValueError("The project type read exceeded its complete-scan limit.")
            symbol = document.GetElement(identifier)
            if not isinstance(symbol, DB.FamilySymbol) or eid(symbol.Family) != eid(family):
                raise ValueError("A project family type could not be verified.")
            symbols.append(symbol)
        result.setdefault(key, []).append((family, symbols))
    return result


def classify_row(row, matches, build, DB):
    result = {"line": row["line"], "existing": None, "sizes": [], "differences": [], "issues": []}
    if not row["family"]:
        return result
    if row["build"] != build:
        result["issues"].append("The project build differs from the reviewed family worker build.")
        return result
    candidates = matches.get(row["family"].casefold(), [])
    exact = [(family, symbols) for family, symbols in candidates
             if str(family.Name) == row["family"] and family.FamilyCategory is not None
             and eid(family.FamilyCategory) == row["category"]["id"]]
    symbol = None
    if candidates and (len(candidates) != 1 or len(exact) != 1):
        result["issues"].append("The project family name or category is ambiguous.")
    elif exact:
        family, symbols = exact[0]
        same = [symbol for symbol in symbols if str(symbol.Name).casefold() == row["type"].casefold()]
        if len(same) == 1 and str(same[0].Name) == row["type"]:
            symbol = same[0]
            result["existing"] = {"family_id": eid(family), "family_uid": str(family.UniqueId),
                                  "symbol_id": eid(symbol), "symbol_uid": str(symbol.UniqueId)}
        elif same:
            result["issues"].append("The requested name collides with a different project type name.")
        else:
            result["issues"].append("The project family already exists. Sibling preservation needs qualification.")
    for size in row["sizes"]:
        field = size["definition"]
        try:
            value = internal_size(DB, size)
        except (KeyError, OverflowError, ValueError) as error:
            result["issues"].append(f"{size['parameter']}: {error}")
            continue
        result["sizes"].append(dict(size, internal_value=value))
        if symbol is not None:
            actual = parameter_state(symbol, field)
            if actual["state"] != "value" or not math.isclose(actual["value"], value, rel_tol=1e-9, abs_tol=1e-9):
                displayed = str(actual["value"]) if actual["state"] == "value" else actual["state"]
                result["differences"].append(f"{size['parameter']}: project {displayed}, requested {value} (internal)")
    return result


def read_project(document, DB, lookup, *, require_unmodified=True):
    validate_record(lookup, "batch-family-type-lookup", "lookup_sha256")
    project = saved_project_identity(document, require_unmodified)
    year = str(document.Application.VersionNumber)
    if year != "2026":
        raise ValueError("Use a saved project in the qualified Revit 2026 build.")
    build = str(document.Application.VersionBuild)
    names = {row["family"].casefold() for row in lookup["rows"] if row["family"]}
    families = matching_families(document, DB, names) if names else {}
    return {"kind": "batch-family-type-project-read", "version": 1,
            "lookup_sha256": lookup["lookup_sha256"], "project": project, "revit_year": year,
            "revit_build": build, "rows": [classify_row(row, families, build, DB) for row in lookup["rows"]]}



def project_query(document, DB, request):
    kept = request.get("kept", False)
    transaction = request.get("transaction")
    if type(kept) is not bool or (kept and not transaction):
        raise ValueError("A kept read needs the exact modify transaction.")
    snapshot = read_project(document, DB, request["lookup"], require_unmodified=not kept)
    result = {"status": "ok", "snapshot_json": json.dumps(snapshot, ensure_ascii=True, allow_nan=False)}
    if request.get("audit_inventory") is not True:
        return result
    ids = request.get("effect_ids", [])
    if (not isinstance(ids, list) or len(ids) > MAX_ROWS + MAX_BATCH_FAMILIES or len(set(ids)) != len(ids)
            or bool(ids) != bool(transaction)):
        raise ValueError("Use the exact transaction name with its provisional element IDs.")
    from System import Int64

    elements = inventory(document, DB)
    for key in ids:
        number = int(key)
        if not 0 < number < 2**63 or str(number) != key:
            raise ValueError("Use canonical positive Revit effect IDs.")
        element = document.GetElement(DB.ElementId(Int64(number)))
        if (element is not None) != kept or (key in elements) != kept:
            raise ValueError("A provisional element's presence differs from the reviewed decision.")
    preserved = {key: value for key, value in elements.items() if key not in ids}
    result.update(audit_token=uuid4().hex, transaction=transaction, inventory=inventory_record(elements),
                  preserved_inventory=inventory_record(preserved),
                  present_ids=sorted(ids, key=int) if kept else [], absent_ids=[] if kept else sorted(ids, key=int))
    return result


try:
    active_project, query_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = project_query(active_project, DB, query_inputs)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        result = {"status": "blocked", "reason": str(error)}
