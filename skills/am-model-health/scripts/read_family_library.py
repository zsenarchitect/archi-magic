"""Read one staged family in a disposable Revit worker with no open documents."""

from contextlib import ExitStack, contextmanager
import json
import math
import os
from time import monotonic

from family_library_discovery import (
    MAX_CATALOG_BYTES, MAX_FAMILIES, MAX_FAMILY_BYTES, MAX_SECONDS, WindowsDirectory,
    directory_info, identity, manifest_digest, pinned_directory, pinned_root, root_path, windows,
)
from read_batch_models import normalized_path
from stage_family_library import check_file_record, parts, verify_pair

MAX_TYPES = 2048
MAX_PARAMETERS = 64
MAX_TEXT = 4096
MAX_RECEIPT = 8 * 1024 * 1024


class WorkerUnresolved(RuntimeError):
    """Discard this worker because its document state could not be restored."""


@contextmanager
def staged_pair(receipt, source_path):
    basename = parts(source_path)[-1]
    basis = {key: value for key, value in receipt.items() if key != "staging_sha256"}
    pairs = receipt["pairs"]
    if (receipt.get("kind") != "family-library-staging" or type(receipt.get("version")) is not int
            or receipt["version"] != 1 or not isinstance(pairs, list) or len(pairs) > MAX_FAMILIES
            or receipt.get("staging_sha256") != manifest_digest(basis)):
        raise ValueError("Use an intact family staging receipt.")
    matches = [pair for pair in pairs if pair["source_path"] == source_path]
    if len(matches) != 1:
        raise ValueError("Select one exact staged family path.")
    row = matches[0]
    directory = row["directory"]
    if not isinstance(directory, str) or len(directory) != 64 or any(c not in "0123456789abcdef" for c in directory):
        raise ValueError("The staging pair directory is malformed.")
    rfa, catalog = row["rfa"], row["catalog"]
    check_file_record(rfa, basename, MAX_FAMILY_BYTES)
    if catalog is not None:
        catalog_name = catalog["basename"]
        if catalog_name.casefold() != (os.path.splitext(basename)[0] + ".txt").casefold():
            raise ValueError("The staging catalog name does not match the family.")
        check_file_record(catalog, catalog_name, MAX_CATALOG_BYTES)

    workspace = root_path(receipt["workspace"])
    path = workspace / directory / basename
    with pinned_root(workspace) as root, pinned_directory(root, directory) as parent, ExitStack() as leaves:
        if (identity(directory_info(root)) != receipt["workspace_identity"]
                or identity(directory_info(parent)) != row["directory_identity"]):
            raise ValueError("A staging directory was replaced.")
        if isinstance(parent, WindowsDirectory):
            for name in (basename, catalog["basename"]) if catalog else (basename,):
                leaves.enter_context(windows.open_handle(parent.path / name, directory=False))
        budget = {"entries": 0, "bytes": 0, "deadline": monotonic() + MAX_SECONDS}
        before = verify_pair(parent, rfa, catalog, budget)
        yield path, row
        budget["deadline"] = monotonic() + MAX_SECONDS
        if verify_pair(parent, rfa, catalog, budget) != before:
            raise ValueError("The staged directory changed during the Revit read.")


def bounded_text(value):
    result = str(value)
    if len(result) > MAX_TEXT:
        raise ValueError("A family metadata field exceeds the text limit.")
    return result


def parameter_values(family_type, parameter, storage):
    readers = {
        "String": family_type.AsString,
        "Integer": family_type.AsInteger,
        "Double": family_type.AsDouble,
        "ElementId": family_type.AsElementId,
    }
    if storage not in readers:
        return {"state": "unsupported"}
    if not family_type.HasValue(parameter):
        return {"state": "unset"}
    raw = readers[storage](parameter)
    if raw is None:
        raise ValueError("An assigned family parameter returned no value.")
    if storage == "String":
        value = bounded_text(raw)
    elif storage == "ElementId":
        value = str(raw.Value)
    elif storage == "Double":
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("A numeric family parameter is outside the finite range.")
    else:
        value = int(raw)
    return {"state": "value", "value": value}


def family_metadata(document, parameter_names):
    family = document.OwnerFamily
    category = family.FamilyCategory
    manager = document.FamilyManager
    parameters = []
    for parameter in manager.Parameters:
        definition = parameter.Definition
        name = str(definition.Name)
        if parameter.IsInstance or name not in parameter_names:
            continue
        if len(parameters) >= MAX_PARAMETERS:
            raise ValueError("The family has too many matching type parameters.")
        parameters.append((parameter, {
            "id": str(parameter.Id.Value),
            "name": name,
            "shared_guid": str(parameter.GUID) if parameter.IsShared else None,
            "storage": str(parameter.StorageType),
            "data_type": bounded_text(definition.GetDataType().TypeId),
        }))
    parameters.sort(key=lambda item: (item[1]["name"], item[1]["id"]))
    if len({record["id"] for _, record in parameters}) != len(parameters):
        raise ValueError("The family has duplicate type parameter IDs.")
    types = {}
    type_bytes = 0
    for family_type in manager.Types:
        name = bounded_text(family_type.Name)
        if name in types or len(types) >= MAX_TYPES:
            raise ValueError("The family has duplicate names or too many embedded types.")
        values = {
            record["id"]: parameter_values(family_type, parameter, record["storage"])
            for parameter, record in parameters
        }
        type_bytes += len(json.dumps({name: values}, ensure_ascii=True, allow_nan=False))
        if type_bytes > MAX_RECEIPT:
            raise ValueError("The family type data exceeds the receipt size limit.")
        types[name] = values
    return {
        "family": bounded_text(family.Name),
        "category": {"id": str(category.Id.Value), "name": bounded_text(category.Name)},
        "parameters": [record for _, record in parameters],
        "embedded_types": [{"name": name, "values": types[name]} for name in sorted(types)],
    }


def read_family(ui, receipt, source_path, parameter_names):
    """Return a receipt only after a successful read, close, and staging recheck."""
    if (not isinstance(parameter_names, (list, tuple)) or len(parameter_names) > MAX_PARAMETERS
            or any(not isinstance(name, str) or not 0 < len(name) <= MAX_TEXT for name in parameter_names)
            or len(set(parameter_names)) != len(parameter_names)):
        raise ValueError("Select a bounded list of distinct type parameter names.")
    app = ui.Application
    if ui.ActiveUIDocument is not None or list(app.Documents):
        raise WorkerUnresolved("Start a disposable Revit worker with no open documents.")
    document = None
    with staged_pair(receipt, source_path) as (path, pair):
        try:
            document = app.OpenDocumentFile(os.fspath(path))
            if (list(app.Documents) != [document] or ui.ActiveUIDocument is not None
                    or not document.IsFamilyDocument or document.IsModified
                    or normalized_path(document.PathName) != normalized_path(path)):
                raise WorkerUnresolved("Revit did not open exactly the staged family.")
            metadata = family_metadata(document, set(parameter_names))
            if document.IsModified or list(app.Documents) != [document] or ui.ActiveUIDocument is not None:
                raise WorkerUnresolved("The Revit worker changed during the metadata read.")
        finally:
            try:
                closed = document is None or document.Close(False)
                restored = not list(app.Documents) and ui.ActiveUIDocument is None
            except Exception as error:
                raise WorkerUnresolved("Revit could not confirm document closure.") from error
            if not closed or not restored:
                raise WorkerUnresolved("Revit did not restore the empty document baseline.")

    result = {
        "kind": "family-library-metadata", "version": 1,
        "staging_sha256": receipt["staging_sha256"], "discovery_sha256": receipt["discovery_sha256"],
        "source_path": source_path, "rfa": pair["rfa"], "catalog": pair["catalog"],
        "catalog_coverage": "pending" if pair["catalog"] else "absent",
        "revit_year": bounded_text(app.VersionNumber), "revit_build": bounded_text(app.VersionBuild),
        "requested_parameters": sorted(parameter_names),
        **metadata,
    }
    result["metadata_sha256"] = manifest_digest(result)
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False)) > MAX_RECEIPT:
        raise ValueError("The family metadata receipt exceeds the size limit.")
    return result
