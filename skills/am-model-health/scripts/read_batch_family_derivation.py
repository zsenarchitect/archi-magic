"""Read the exact active writable family copy before deriving new batch types."""

import json
from uuid import uuid4

from family_library_discovery import manifest_digest
from prepare_batch_family_derivation import verified_working_copy
from read_batch_models import normalized_path
from read_family_placement import inventory, inventory_record
from read_family_library import MAX_TYPES, bounded_text, parameter_values

MAX_PARAMETERS = 250
MAX_STATE_BYTES = 4 * 1024 * 1024


def definition_record(parameter):
    definition = parameter.Definition
    return {
        "id": str(parameter.Id.Value), "name": bounded_text(definition.Name),
        "shared_guid": str(parameter.GUID) if parameter.IsShared else None,
        "storage": str(parameter.StorageType), "data_type": bounded_text(definition.GetDataType().TypeId),
        "read_only": bool(parameter.IsReadOnly), "formula_driven": bool(parameter.IsDeterminedByFormula),
        "reporting": bool(parameter.IsReporting), "instance": bool(parameter.IsInstance),
        "formula": bounded_text(parameter.Formula) if parameter.IsDeterminedByFormula else None,
    }


def family_state(document):
    manager = document.FamilyManager
    bindings = {}
    for parameter in manager.Parameters:
        field = definition_record(parameter)
        if field["id"] in bindings or len(bindings) >= MAX_PARAMETERS:
            raise ValueError("The working family exceeded the complete parameter inventory limit.")
        bindings[field["id"]] = (parameter, field)
    types = {}
    names = set()
    size = 0
    for family_type in manager.Types:
        name = bounded_text(family_type.Name)
        if name.casefold() in names or len(types) >= MAX_TYPES:
            raise ValueError("The working family has colliding names or too many types.")
        names.add(name.casefold())
        values = {identifier: parameter_values(family_type, parameter, field["storage"])
                  for identifier, (parameter, field) in bindings.items()}
        size += len(json.dumps({name: values}, ensure_ascii=True, allow_nan=False))
        if size > MAX_STATE_BYTES:
            raise ValueError("The working family state exceeds 4 MiB.")
        types[name] = values
    return {
        "parameters": {identifier: field for identifier, (_, field) in bindings.items()},
        "types": types,
        "current_type": bounded_text(manager.CurrentType.Name) if manager.CurrentType is not None else None,
    }


def read_working_family(document, plan, DB=None):
    with verified_working_copy(plan) as path:
        if (document is None or not document.IsFamilyDocument or document.IsReadOnly or document.IsModified
                or document.IsWorkshared or document.IsModelInCloud or document.IsModifiable
                or not document.PathName or normalized_path(document.PathName) != normalized_path(path)
                or str(document.Application.VersionNumber) != "2026"
                or str(document.Application.VersionBuild) != plan["revit_build"]):
            raise ValueError("Open the exact saved writable family copy in the reviewed Revit 2026 build.")
        family = document.OwnerFamily
        category = family.FamilyCategory
        identity = {"family": bounded_text(family.Name), "category_id": str(category.Id.Value)}
        owner = {"id": str(family.Id.Value), "uid": str(family.UniqueId)}
        state = family_state(document)
        names = {name.casefold() for name in state["types"]}
        for row in plan["rows"]:
            if identity != {"family": row["family"], "category_id": row["category"]["id"]}:
                raise ValueError("The working family name or category differs from the reviewed source.")
            if row["seed_type"] not in state["types"] or row["type"].casefold() in names:
                raise ValueError("A reviewed seed is missing or the requested type already exists.")
            for size in row["sizes"]:
                expected = size["definition"]
                actual = state["parameters"].get(expected["id"])
                if (actual is None or actual["instance"] or actual["read_only"] or actual["formula_driven"]
                        or actual["reporting"] or actual["storage"] != "Double"
                        or any(actual[key] != value for key, value in expected.items())):
                    raise ValueError(f"The reviewed size parameter {size['parameter']} changed or is not writable.")
        baseline_inventory = inventory_record(inventory(document, DB)) if DB is not None else None
        if document.IsModified:
            raise ValueError("The active family changed during the read.")
    receipt = {"kind": "batch-family-working-read", "version": 1,
               "derivation_sha256": plan["derivation_sha256"], "revit_build": plan["revit_build"],
               **identity, "owner": owner, "state": state, "inventory": baseline_inventory}
    return dict(receipt, working_read_sha256=manifest_digest(receipt))


try:
    active_family, query_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        read = read_working_family(active_family, query_inputs["derivation"], DB)
        result = {"status": "ok", "snapshot_json": json.dumps(read, ensure_ascii=True, allow_nan=False),
                  "audit_token": uuid4().hex, "trial_transaction": query_inputs.get("trial_transaction")}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "blocked", "reason": str(error)}
