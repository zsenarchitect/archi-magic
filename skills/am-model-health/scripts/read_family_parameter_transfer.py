"""Read protected family parameters and reference evidence for a transfer review."""

import json
import math
import os

from family_library_discovery import manifest_digest
from family_transfer_nested import nested_families
from read_batch_family_derivation import family_state
from read_batch_models import normalized_path
from read_family_library import WorkerUnresolved, bounded_text, staged_pair, worker_document
from read_family_placement import MAX_ELEMENTS, MAX_SYMBOLS, eid, inventory, saved_project_identity

MAX_REFERENCES = 5000
MAX_ISSUES = 100
MAX_STATE_BYTES = 4 * 1024 * 1024


def built_in(DB, definition):
    if isinstance(definition, DB.InternalDefinition) and definition.BuiltInParameter != DB.BuiltInParameter.INVALID:
        return str(definition.BuiltInParameter)
    return None


def association(document, DB, parameter):
    owner = parameter.Element
    if owner is None or not owner.Document.Equals(document):
        raise ValueError("A family association has no local owning element.")
    definition = parameter.Definition
    return {"owner_id": eid(owner), "owner_uid": bounded_text(owner.UniqueId), "parameter_id": eid(parameter),
            "name": bounded_text(definition.Name), "data_type": bounded_text(definition.GetDataType().TypeId),
            "built_in": built_in(DB, definition),
            "shared_guid": str(parameter.GUID) if parameter.IsShared else None}


def dimension_record(document, dimension, parameters, count):
    label = dimension.FamilyLabel
    identifier = eid(label) if label is not None else None
    if identifier is not None and identifier not in parameters:
        raise ValueError("A dimension label does not resolve to a family definition.")
    references = []
    for reference in dimension.References:
        count()
        owner = document.GetElement(reference.ElementId)
        if owner is None:
            raise ValueError("A dimension reference does not resolve to a local element.")
        references.append({"owner_id": eid(owner), "owner_uid": bounded_text(owner.UniqueId),
                           "stable_reference": bounded_text(reference.ConvertToStableRepresentation(document))})
    return {"id": eid(dimension), "uid": bounded_text(dimension.UniqueId), "label_id": identifier,
            "references": references}


class ReferenceLimit(ValueError):
    pass


def transfer_state(document, DB, *, inspect_trial=False):
    if (not document.IsFamilyDocument or ((document.IsModified or document.IsModifiable) and not inspect_trial)
            or document.IsWorkshared or document.IsModelInCloud):
        raise ValueError("Use an unmodified standalone family document for the inventory.")
    state = family_state(document)
    coverage = {"complete": True, "issues": []}
    count = 0

    def issue(kind, identifier, error):
        coverage["complete"] = False
        if len(coverage["issues"]) < MAX_ISSUES:
            coverage["issues"].append({"kind": kind, "id": identifier, "error": type(error).__name__})

    def next_reference():
        nonlocal count
        count += 1
        if count > MAX_REFERENCES:
            raise ReferenceLimit("The complete family reference inventory exceeds 5,000 records.")

    definitions = {}
    dimensions = []
    try:
        for parameter in document.FamilyManager.Parameters:
            key = eid(parameter)
            if key not in state["parameters"] or key in definitions:
                raise ValueError("The family definitions changed during inspection.")
            definition = parameter.Definition
            associations = []
            record = dict(state["parameters"][key], group=bounded_text(definition.GetGroupTypeId().TypeId),
                          built_in=built_in(DB, definition), can_assign_formula=bool(parameter.CanAssignFormula),
                          yes_no=definition.GetDataType() == DB.SpecTypeId.Boolean.YesNo,
                          associations=associations)
            definitions[key] = record
            try:
                for target in parameter.AssociatedParameters:
                    next_reference()
                    associations.append(association(document, DB, target))
            except ReferenceLimit:
                raise
            except Exception as error:
                issue("parameter_association", key, error)
            associations.sort(key=lambda row: (row["owner_uid"], row["parameter_id"]))
        if set(definitions) != set(state["parameters"]):
            raise ValueError("The family definitions changed during inspection.")
        for dimension in DB.FilteredElementCollector(document).OfClass(DB.Dimension):
            next_reference()
            try:
                dimensions.append(dimension_record(document, dimension, definitions, next_reference))
            except ReferenceLimit:
                raise
            except Exception as error:
                issue("dimension_reference", eid(dimension), error)
    except ReferenceLimit as error:
        issue("reference_limit", None, error)
    if (document.IsModified or document.IsModifiable) and not inspect_trial:
        raise ValueError("The family changed during the read.")
    result = {"definitions": definitions, "types": state["types"], "current_type": state["current_type"],
              "dimensions": sorted(dimensions, key=lambda row: row["uid"]),
              "reference_scope": "family_document", "coverage": coverage}
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False)) > MAX_STATE_BYTES:
        raise ValueError("The complete family inventory exceeds 4 MiB.")
    return result


def snapshot(document, DB, *, kept=False):
    if (document is None or not document.IsFamilyDocument or (document.IsModified and not kept) or document.IsModifiable
            or str(document.Application.VersionNumber) != "2026"):
        raise ValueError("Open the exact unmodified family in Revit 2026.")
    family = document.OwnerFamily
    category = family.FamilyCategory
    if category is None:
        raise ValueError("The family category is unavailable.")
    return {"family": bounded_text(family.Name), "category_id": eid(category),
            "revit_build": bounded_text(document.Application.VersionBuild), "state": transfer_state(document, DB, inspect_trial=kept)}


def staged_source(ui, DB, request):
    receipt = request["staging"]
    relative = request["source_path"]
    with staged_pair(receipt, relative) as (path, pair):
        with worker_document(ui, lambda app: app.OpenDocumentFile(os.fspath(path))) as document:
            if normalized_path(document.PathName) != normalized_path(path):
                raise ValueError("Revit did not open the exact staged family.")
            read = snapshot(document, DB)
            if read["revit_build"] != request["revit_build"]:
                raise ValueError("The staged family was read in a different Revit build.")
    return {"kind": "family-parameter-transfer-read", "version": 1, "role": "source",
            "source": {"path": relative, "staging_sha256": receipt["staging_sha256"],
                       "rfa": pair["rfa"], "catalog": pair["catalog"]}, **read}


def project_values(document, DB, family):
    """Capture the exact loaded type and instance values within the project identity."""
    symbols = []
    instances = []
    total = 0

    def describe(element, symbol):
        nonlocal total
        values = {}
        for parameter in element.Parameters:
            total += 1
            identifier = eid(parameter)
            if total > MAX_REFERENCES or identifier in values:
                raise ValueError("The project parameter inventory is incomplete or exceeds its limit.")
            field = association(document, DB, parameter)
            storage = str(parameter.StorageType)
            field["storage"] = storage
            if not parameter.HasValue:
                field["state"] = "unset"
            else:
                if storage == "String":
                    raw = parameter.AsString()
                    value = bounded_text(raw) if raw is not None else None
                elif storage == "Double":
                    value = float(parameter.AsDouble())
                    if not math.isfinite(value):
                        raise ValueError("A project parameter has a nonfinite value.")
                elif storage == "Integer":
                    value = int(parameter.AsInteger())
                elif storage == "ElementId":
                    value = str(parameter.AsElementId().Value)
                else:
                    raise ValueError("A valued project parameter has unsupported storage.")
                field.update(state="value", value=value)
            values[identifier] = field
        return {"id": eid(element), "uid": bounded_text(element.UniqueId), "type_id": eid(symbol),
                "type_name": bounded_text(symbol.Name), "parameters": values}

    for position, identifier in enumerate(family.GetFamilySymbolIds()):
        if position >= MAX_SYMBOLS:
            raise ValueError("The project type inventory exceeds its complete-scan limit.")
        symbol = document.GetElement(identifier)
        if not isinstance(symbol, DB.FamilySymbol) or eid(symbol.Family) != eid(family):
            raise ValueError("An existing loaded family type cannot be identified.")
        symbols.append(describe(symbol, symbol))
    for position, instance in enumerate(DB.FilteredElementCollector(document).OfClass(DB.FamilyInstance)):
        if position >= MAX_ELEMENTS:
            raise ValueError("The project instance inventory exceeds its complete-scan limit.")
        symbol = instance.Symbol
        if symbol is not None and eid(symbol.Family) == eid(family):
            instances.append(describe(instance, symbol))
    return {"types": sorted(symbols, key=lambda row: int(row["id"])),
            "instances": sorted(instances, key=lambda row: int(row["id"]))}


def inspect_loaded(ui, DB, document, family):
    """Inspect one loaded family while the project is nonmodifiable."""
    app = ui.Application
    active = ui.ActiveUIDocument
    if (document.IsModifiable or active is None or not active.Document.Equals(document)
            or list(app.Documents) != [document]):
        raise ValueError("Close the project transaction before inspecting the loaded family.")
    expected = (str(family.Name), eid(family.FamilyCategory))
    inspected = None
    try:
        opened = document.EditFamily(family)
        if opened is None or opened.Equals(document):
            raise WorkerUnresolved("Revit did not return an independent inspection family.")
        inspected = opened
        current = list(app.Documents)
        if len(current) != 2 or document not in current or inspected not in current:
            raise WorkerUnresolved("Revit opened an unexpected document during family inspection.")
        if ui.ActiveUIDocument is None or not ui.ActiveUIDocument.Document.Equals(document):
            raise WorkerUnresolved("The active project changed during family inspection.")
        read = snapshot(inspected, DB)
        read["nested_families"] = nested_families(ui, DB, inspected)
        if (read["family"], read["category_id"]) != expected:
            raise ValueError("The inspected family differs from the loaded project family.")
    finally:
        try:
            closed = inspected is None or inspected.Close(False)
            restored = (list(app.Documents) == [document] and ui.ActiveUIDocument is not None
                        and ui.ActiveUIDocument.Document.Equals(document))
        except Exception as error:
            raise WorkerUnresolved("Revit could not confirm inspection family closure.") from error
        if not closed or not restored:
            raise WorkerUnresolved("Revit did not restore the exact original open project.")
    return read


def loaded_target(ui, DB, document, request):
    project = saved_project_identity(document)
    app = ui.Application
    active = ui.ActiveUIDocument
    if (document.IsModifiable or active is None or not active.Document.Equals(document)
            or list(app.Documents) != [document]
            or str(app.VersionNumber) != "2026" or str(app.VersionBuild) != request["revit_build"]):
        raise ValueError("Use the exact saved standalone project as the only open document in the reviewed build.")
    key = request["family_id"]
    value = int(key)
    if value <= 0 or str(value) != key:
        raise ValueError("Use an exact positive project family ID string.")
    family = document.GetElement(DB.ElementId(value))
    if (not isinstance(family, DB.Family) or str(family.UniqueId) != request["family_uid"]
            or not family.IsEditable or family.IsInPlace or family.FamilyCategory is None):
        raise ValueError("The exact editable loaded project family is unavailable.")
    baseline = inventory(document, DB)
    overrides = project_values(document, DB, family)
    read = inspect_loaded(ui, DB, document, family)
    if (document.IsModified or document.IsModifiable or inventory(document, DB) != baseline
            or project_values(document, DB, family) != overrides):
        raise WorkerUnresolved("The project changed during the read-only family inspection.")
    result = {"kind": "family-parameter-transfer-read", "version": 1, "role": "target",
              "project": project, "loaded_family": {"id": key, "uid": str(family.UniqueId)},
              "project_values": overrides, **read}
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False)) > MAX_STATE_BYTES:
        raise ValueError("The combined family and project inventory exceeds 4 MiB.")
    return result


def read_result(ui, DB, active, request):
    role = request["role"]
    if role == "source":
        read = staged_source(ui, DB, request)
    elif role == "target":
        read = loaded_target(ui, DB, active, request)
    else:
        raise ValueError("Select source or target family inspection.")
    read["read_sha256"] = manifest_digest(read)
    return read


try:
    active_project, query_inputs, current_ui = doc, inputs, uiapp
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        read = read_result(current_ui, DB, active_project, query_inputs)
        result = {"status": "ok" if read["state"]["coverage"]["complete"] else "partial",
                  "snapshot_json": json.dumps(read, ensure_ascii=True, allow_nan=False)}
    except WorkerUnresolved as error:
        result = {"status": "worker_unresolved", "reason": str(error)}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "blocked", "reason": str(error)}
