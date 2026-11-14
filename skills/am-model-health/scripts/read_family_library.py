#! python3
"""Read one staged family in a disposable Revit worker with no open documents."""

import argparse
from io import BytesIO
from contextlib import ExitStack, contextmanager
import json
import math
import os
import platform
import sys
from time import monotonic
from uuid import uuid4

from family_library_discovery import (
    MAX_CATALOG_BYTES, MAX_FAMILIES, MAX_FAMILY_BYTES, MAX_SECONDS, WindowsDirectory,
    checked_file, child_info, directory_info, identity, manifest_digest, pinned_directory, pinned_root, root_path, windows,
)
from family_library_catalog import catalog_candidates
from family_library_thumbnails import MAX_IMAGE_BYTES, exact_symbol, png_record, preview_bytes, render_thumbnails
from read_family_placement import parameter_state
from read_batch_models import normalized_path
from stage_family_library import MAX_MANIFEST, check_file_record, make_directory, new_file, parts, verify_pair

MAX_TYPES = 250
MAX_PARAMETERS = 50
MAX_SOURCE_SECONDS = 60
MAX_TEXT = 4096
MAX_RECEIPT = 8 * 1024 * 1024
WORKER_DIRECTORY_ENV = "ARCHI_MAGIC_FAMILY_WORKER_DIR"


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
            "read_only": bool(parameter.IsReadOnly), "formula_driven": bool(parameter.IsDeterminedByFormula),
            "reporting": bool(parameter.IsReporting),
        }))
    parameters.sort(key=lambda item: (item[1]["name"], item[1]["id"]))
    if len({record["id"] for _, record in parameters}) != len(parameters):
        raise ValueError("The family has duplicate type parameter IDs.")
    types = {}
    type_bytes = 0
    coverage = "complete"
    for family_type in manager.Types:
        name = bounded_text(family_type.Name)
        if len(types) >= MAX_TYPES:
            coverage = "type_limit"
            break
        if name in types:
            raise ValueError("The family has duplicate type names.")
        values = {
            record["id"]: parameter_values(family_type, parameter, record["storage"])
            for parameter, record in parameters
        }
        type_bytes += len(json.dumps({name: values}, ensure_ascii=True, allow_nan=False))
        if type_bytes > MAX_RECEIPT // 2:
            coverage = "size_limit"
            break
        types[name] = values
    return {
        "family": bounded_text(family.Name),
        "placement_type": bounded_text(family.FamilyPlacementType),
        "category": {"id": str(category.Id.Value), "name": bounded_text(category.Name)},
        "parameters": [record for _, record in parameters],
        "embedded_type_coverage": coverage,
        "embedded_types": [{"name": name, "values": types[name]} for name in sorted(types)],
    }


def validate_parameter_names(parameter_names):
    if (not isinstance(parameter_names, (list, tuple)) or len(parameter_names) > MAX_PARAMETERS
            or any(not isinstance(name, str) or not 0 < len(name) <= MAX_TEXT for name in parameter_names)
            or len(set(parameter_names)) != len(parameter_names)):
        raise ValueError("Select a bounded list of distinct type parameter names.")


@contextmanager
def worker_document(ui, open_document):
    app = ui.Application
    if ui.ActiveUIDocument is not None or list(app.Documents):
        raise WorkerUnresolved("Start a disposable Revit worker with no open documents.")
    document = None
    try:
        document = open_document(app)
        if document is None or list(app.Documents) != [document] or ui.ActiveUIDocument is not None:
            raise WorkerUnresolved("Revit did not open exactly one background document.")
        yield document
        if list(app.Documents) != [document] or ui.ActiveUIDocument is not None:
            raise WorkerUnresolved("The Revit worker changed while the document was open.")
    finally:
        try:
            closed = document is None or document.Close(False)
            restored = not list(app.Documents) and ui.ActiveUIDocument is None
        except Exception as error:
            raise WorkerUnresolved("Revit could not confirm document closure.") from error
        if not closed or not restored:
            raise WorkerUnresolved("Revit did not restore the empty document baseline.")


def read_family(ui, receipt, source_path, parameter_names):
    """Return a receipt only after a successful read, close, and staging recheck."""
    validate_parameter_names(parameter_names)
    app = ui.Application
    with staged_pair(receipt, source_path) as (path, pair):
        with worker_document(ui, lambda app: app.OpenDocumentFile(os.fspath(path))) as document:
            if (not document.IsFamilyDocument or document.IsModified
                    or normalized_path(document.PathName) != normalized_path(path)):
                raise WorkerUnresolved("Revit did not open exactly the staged family.")
            metadata = family_metadata(document, set(parameter_names))
            if document.IsModified:
                raise WorkerUnresolved("The staged family was modified during the metadata read.")

    result = {
        "kind": "family-library-metadata", "version": 1,
        "staging_sha256": receipt["staging_sha256"], "discovery_sha256": receipt["discovery_sha256"],
        "source_path": source_path, "rfa": pair["rfa"], "catalog": pair["catalog"],
        "catalog_coverage": "pending" if pair["catalog"] else "absent",
        "catalog_types": [], "catalog_issues": [], "catalog_blocked_embedded": [],
        "revit_year": bounded_text(app.VersionNumber), "revit_build": bounded_text(app.VersionBuild),
        "requested_parameters": sorted(parameter_names),
        **metadata,
    }
    result["metadata_sha256"] = manifest_digest(result)
    if len(json.dumps(result, ensure_ascii=True, allow_nan=False)) > MAX_RECEIPT:
        raise ValueError("The family metadata receipt exceeds the size limit.")
    return result


def capture_thumbnails(ui, path, metadata, deadline):
    names = [row["name"] for row in metadata["embedded_types"]]
    if not names:
        return [], {}
    if metadata["catalog"] is not None:
        return [{"type": name, "status": "catalog_pending"} for name in names], {}
    if monotonic() >= deadline:
        return [{"type": name, "status": "time_limit"} for name in names], {}

    from pyrevit.api import DB
    with worker_document(ui, lambda app: app.NewProjectDocument(DB.UnitSystem.Metric)) as scratch:
        if scratch.IsFamilyDocument or scratch.PathName or scratch.IsWorkshared or scratch.IsModelInCloud:
            raise WorkerUnresolved("Revit did not create an unsaved scratch project.")
        return render_thumbnails(scratch, DB, path, metadata, deadline)


def catalog_scratch(ui, path, metadata, candidates, deadline):
    from pyrevit.api import DB

    types, thumbnails, issues = [], [], []
    assets = {}
    with worker_document(ui, lambda app: app.NewProjectDocument(DB.UnitSystem.Metric)) as scratch:
        if scratch.IsFamilyDocument or scratch.PathName or scratch.IsWorkshared or scratch.IsModelInCloud:
            raise WorkerUnresolved("Revit did not create an unsaved scratch project for the catalog.")
        families = DB.FilteredElementCollector(scratch).OfClass(DB.Family)
        if any(str(family.Name).casefold() == metadata["family"].casefold() for family in families):
            return [], [], {}, [{"reason": "scratch_name_collision"}]
        for name in candidates:
            if monotonic() >= deadline:
                issues.append({"reason": "time_limit"})
                break
            symbol = exact_symbol(scratch, DB, path, name, metadata)
            if symbol is None:
                issues.append({"type": name, "reason": "native_type_unavailable"})
                continue
            values = {field["id"]: parameter_state(symbol, field) for field in metadata["parameters"]}
            types.append({"name": name, "values": values})
            row = {"type": name, "status": "preview_unavailable"}
            data = preview_bytes(symbol)
            if data is not None:
                try:
                    image = png_record(data)
                except ValueError:
                    row["status"] = "invalid_preview"
                else:
                    row.update(status="available", image=image)
                    assets[image["file"]] = data
            thumbnails.append(row)
    return types, thumbnails, assets, issues


def capture_catalog(ui, path, metadata, deadline, native=catalog_scratch):
    catalog = metadata["catalog"]
    data = BytesIO()
    with pinned_root(path.parent) as parent:
        name = catalog["basename"]
        checked = checked_file(parent, name, child_info(parent, name), MAX_CATALOG_BYTES,
                               {"bytes": 0, "deadline": monotonic() + MAX_SECONDS}, data)
        if checked != catalog:
            raise ValueError("The staged catalog changed before qualification.")
    embedded = [row["name"] for row in metadata["embedded_types"]]
    names, blocked, issues = catalog_candidates(data.getvalue(), embedded, MAX_TYPES - len(embedded))
    types, thumbnails, assets = [], [], {}
    if names:
        types, thumbnails, assets, native_issues = native(ui, path, metadata, names, deadline)
        issues.extend(native_issues)
        verified = [row["name"] for row in types]
        if len(set(verified)) != len(verified) or set(verified) - set(names):
            raise ValueError("The worker returned an unreviewed or duplicate catalog type.")
        if set(verified) != set(names) and not native_issues:
            issues.append({"reason": "native_coverage_incomplete"})
    elif not issues:
        issues.append({"reason": "empty_catalog"})
    updated = dict(metadata, catalog_types=types, catalog_blocked_embedded=blocked, catalog_issues=issues,
                   catalog_coverage="complete" if not issues else "partial")
    updated.pop("metadata_sha256")
    updated["metadata_sha256"] = manifest_digest(updated)
    return updated, thumbnails, assets


def verify_thumbnail_assets(parent, records, metadata, requested):
    if not requested:
        if records:
            raise ValueError("A metadata-only request returned thumbnails.")
        return
    embedded = [row["name"] for row in metadata["embedded_types"]]
    catalog = [row["name"] for row in metadata["catalog_types"]]
    expected = embedded + catalog
    if len(expected) > MAX_TYPES or len(set(expected)) != len(expected) or [row["type"] for row in records] != expected:
        raise ValueError("The thumbnail rows do not match the exact verified types.")
    if metadata["catalog"] is not None and any(row["status"] != "catalog_pending" for row in records[:len(embedded)]):
        raise ValueError("The paired embedded types require separate thumbnail qualification.")
    checked = {}
    for row in records:
        if row["status"] in {"scratch_name_collision", "time_limit", "preview_unavailable",
                             "type_unavailable", "invalid_preview", "catalog_pending"} and set(row) == {"type", "status"}:
            continue
        if row["status"] != "available" or set(row) != {"type", "status", "image"}:
            raise ValueError("A thumbnail row has invalid coverage.")
        image = row["image"]
        digest = image["sha256"]
        if (not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or image["file"] != f"thumbnail-{digest}.png"):
            raise ValueError("A thumbnail has an invalid content address.")
        name = image["file"]
        if name not in checked:
            data = BytesIO()
            checked_file(parent, name, child_info(parent, name), MAX_IMAGE_BYTES,
                         {"bytes": 0, "deadline": monotonic() + MAX_SECONDS}, data)
            checked[name] = png_record(data.getvalue())
        if checked[name] != image:
            raise ValueError("A thumbnail does not match its receipt.")


def read_request_file(parent, name):
    budget = {"bytes": 0, "deadline": monotonic() + MAX_SECONDS}
    data = BytesIO()
    checked_file(parent, name, child_info(parent, name), MAX_MANIFEST, budget, data)
    return json.loads(data.getvalue())


def encode_record(record):
    data = json.dumps(record, ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(data) > MAX_RECEIPT:
        raise ValueError("The worker record exceeds the size limit.")
    return data


def prepare_worker(receipt, source_path, parameter_names, revit_year, revit_build, pyrevit_build, *, thumbnails=True, generation=None):
    """Make a private, one-use native worker request for an exact source pair."""
    validate_parameter_names(parameter_names)
    if (type(thumbnails) is not bool or revit_year != "2026"
            or any(not isinstance(build, str) or not build.strip() or len(build) > 100
                   for build in (revit_build, pyrevit_build))):
        raise ValueError("Supply the expected Revit 2026 and pyRevit build identifiers.")
    if generation is not None and (not isinstance(generation, str) or len(generation) != 32
                                   or any(c not in "0123456789abcdef" for c in generation)):
        raise ValueError("Use the refresh token from the family index.")
    workspace = root_path(receipt["workspace"])
    name = "metadata-worker-" + uuid4().hex
    with staged_pair(receipt, source_path):
        pass
    with pinned_root(workspace) as parent:
        if identity(directory_info(parent)) != receipt["workspace_identity"]:
            raise ValueError("The staging workspace was replaced.")
        make_directory(parent, name)
        with pinned_directory(parent, name) as directory:
            request = {
                "kind": "family-library-worker-request", "version": 1,
                "directory_identity": identity(directory_info(directory)),
                "source_path": source_path, "parameter_names": sorted(parameter_names),
                "revit_year": revit_year, "revit_build": revit_build, "pyrevit_build": pyrevit_build,
                "staging": receipt, "thumbnails": thumbnails, "generation": generation,
            }
            request["request_sha256"] = manifest_digest(request)
            data = encode_record(request)
            with new_file(directory, "request.json") as stream:
                stream.write(data)
    return workspace / name


def worker_request(parent, directory):
    request = read_request_file(parent, "request.json")
    basis = {key: value for key, value in request.items() if key != "request_sha256"}
    if (request.get("kind") != "family-library-worker-request" or request.get("version") != 1
            or request.get("request_sha256") != manifest_digest(basis)
            or type(request.get("thumbnails")) is not bool
            or request["directory_identity"] != identity(directory_info(parent))
            or directory.parent != root_path(request["staging"]["workspace"])):
        raise ValueError("The family worker request or its directory changed.")
    return request


def run_worker(ui, runtime, raw_directory, thumbnail_reader=capture_thumbnails, catalog_reader=capture_catalog):
    """Consume one prepared request and write a receipt after the worker is empty."""
    directory = root_path(raw_directory)
    with pinned_root(directory) as parent:
        request = worker_request(parent, directory)
        app = ui.Application
        if (not runtime or str(runtime) != request["pyrevit_build"]
                or str(app.VersionNumber) != request["revit_year"]
                or str(app.VersionBuild) != request["revit_build"]):
            raise ValueError("The loaded Revit or pyRevit build differs from the request.")
        with new_file(parent, "worker.started") as stream:
            stream.write(request["request_sha256"].encode("ascii"))
        deadline = monotonic() + MAX_SOURCE_SECONDS
        metadata = read_family(ui, request["staging"], request["source_path"], request["parameter_names"])
        thumbnails, assets = [], {}
        if request["thumbnails"]:
            with staged_pair(request["staging"], request["source_path"]) as (path, _):
                if metadata["catalog"] is None:
                    thumbnails, assets = thumbnail_reader(ui, path, metadata, deadline)
                else:
                    thumbnails = [{"type": row["name"], "status": "catalog_pending"}
                                  for row in metadata["embedded_types"]]
                    metadata, catalog_thumbnails, assets = catalog_reader(ui, path, metadata, deadline)
                    thumbnails.extend(catalog_thumbnails)
        for name, data in assets.items():
            if png_record(data)["file"] != name:
                raise ValueError("A thumbnail asset name does not match its bytes.")
            with new_file(parent, name) as stream:
                stream.write(data)
        verify_thumbnail_assets(parent, thumbnails, metadata, request["thumbnails"])
        result = {
            "kind": "family-library-worker-receipt", "version": 1,
            "request_sha256": request["request_sha256"],
            "runtime": {"pyrevit_build": str(runtime), "engine": platform.python_implementation(),
                        "engine_version": platform.python_version()},
            "metadata": metadata, "thumbnails": thumbnails, "generation": request["generation"],
        }
        result["receipt_sha256"] = manifest_digest(result)
        data = encode_record(result)
        with new_file(parent, "receipt.json") as stream:
            stream.write(data)


def collect_worker(raw_directory):
    """Read a complete receipt for this request and confirm its staged inputs."""
    directory = root_path(raw_directory)
    with pinned_root(directory) as parent:
        request = worker_request(parent, directory)
        result = read_request_file(parent, "receipt.json")
        basis = {key: value for key, value in result.items() if key != "receipt_sha256"}
        if (result.get("kind") != "family-library-worker-receipt" or result.get("version") != 1
                or result.get("request_sha256") != request["request_sha256"]
                or result.get("receipt_sha256") != manifest_digest(basis)
                or result["runtime"]["pyrevit_build"] != request["pyrevit_build"]
                or result.get("generation") != request["generation"]):
            raise ValueError("The family worker receipt is incomplete or belongs to a different request.")
        metadata = result["metadata"]
        staging = request["staging"]
        basis = {key: value for key, value in metadata.items() if key != "metadata_sha256"}
        with staged_pair(staging, request["source_path"]) as (_, pair):
            if (metadata.get("kind") != "family-library-metadata" or metadata.get("version") != 1
                    or metadata.get("metadata_sha256") != manifest_digest(basis)
                    or metadata["staging_sha256"] != staging["staging_sha256"]
                    or metadata["discovery_sha256"] != staging["discovery_sha256"]
                    or metadata["source_path"] != request["source_path"]
                    or metadata["requested_parameters"] != request["parameter_names"]
                    or metadata["revit_year"] != request["revit_year"]
                    or metadata["revit_build"] != request["revit_build"]
                    or metadata["rfa"] != pair["rfa"] or metadata["catalog"] != pair["catalog"]):
                raise ValueError("The metadata does not match the requested build and staged family.")
            verify_thumbnail_assets(parent, result["thumbnails"], metadata, request["thumbnails"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help=prepare_worker.__doc__)
    prepare.add_argument("staging_receipt")
    prepare.add_argument("--path", required=True, help="Exact staged RFA path")
    prepare.add_argument("--generation", help="Refresh token from the family index")
    prepare.add_argument("--metadata-only", action="store_true")
    prepare.add_argument("--parameter", action="append", default=[])
    prepare.add_argument("--revit-build", required=True)
    prepare.add_argument("--pyrevit-build", required=True)
    collect = commands.add_parser("collect", help=collect_worker.__doc__)
    collect.add_argument("worker_directory")
    args = parser.parse_args()
    try:
        if args.command == "collect":
            print(encode_record(collect_worker(args.worker_directory)).decode("ascii"))
        else:
            if os.name == "nt" and sys.version_info < (3, 13):
                parser.error("Use Python 3.13 or newer to create a private Windows worker directory.")
            staging = root_path(args.staging_receipt)
            with pinned_root(staging.parent) as parent:
                receipt = read_request_file(parent, staging.name)
            directory = prepare_worker(receipt, args.path, args.parameter, "2026", args.revit_build,
                                       args.pyrevit_build, thumbnails=not args.metadata_only, generation=args.generation)
            print(directory)
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        batch = __batchexec__
    except NameError:
        batch = False
    if batch:
        if os.name != "nt" or platform.python_implementation() != "CPython":
            raise RuntimeError("Use the CPython engine in a disposable Windows Revit batch host.")
        if __models__:
            raise RuntimeError("Run the family worker with no model arguments.")
        from pyrevit import versionmgr
        run_worker(__revit__, versionmgr.get_loaded_build_version(), os.environ[WORKER_DIRECTORY_ENV])
    else:
        sys.exit(main())
