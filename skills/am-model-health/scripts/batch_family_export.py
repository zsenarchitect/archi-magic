#! python3
"""Reserve, reopen, and seal a privately exported batch family RFA."""

import argparse
from contextlib import ExitStack, contextmanager
import os
import platform
import sys
from time import monotonic
from uuid import uuid4

from batch_family_type_preview import validate_record
from family_library_discovery import (
    MAX_FAMILY_BYTES, MAX_SECONDS, WindowsDirectory, checked_file, child_info, directory_info, discover,
    identity, manifest_digest, pinned_directory, pinned_root, root_path, windows,
)
from family_placement_preview import load_json
from prepare_batch_family_derivation import verified_working_copy
from read_batch_family_derivation import family_state
from read_batch_models import normalized_path
from read_family_library import encode_record, read_request_file, staged_pair, worker_document
from stage_family_library import HEADROOM, free_bytes, make_directory, new_file, parts, verify_pair

EXPORT_ENV = "ARCHI_MAGIC_FAMILY_EXPORT_DIR"


def reserve_export(plan, decision, raw_destination):
    validate_record(decision, "batch-family-derivation-decision", "decision_sha256")
    if decision["status"] != "kept_verified" or decision["derivation_sha256"] != plan["derivation_sha256"]:
        raise ValueError("Use a verified user Keep for this exact derivation.")
    if os.name == "nt" and sys.version_info < (3, 13):
        raise ValueError("Use Python 3.13 or newer for a private Windows export.")
    destination = root_path(raw_destination)
    name = "family-export-" + uuid4().hex
    workspace = destination / name
    ancestry = []
    with verified_working_copy(plan) as working, pinned_root(destination, ancestry=ancestry) as parent:
        if any(plan[key] in ancestry for key in ("root_identity", "workspace_identity", "staging_workspace_identity")):
            raise ValueError("Reserve the export outside the source library and working copy.")
        if free_bytes(parent) - MAX_FAMILY_BYTES < HEADROOM:
            raise ValueError("The reserved export would leave less than 1 GiB free.")
        make_directory(parent, name)
        with pinned_directory(parent, name) as directory:
            make_directory(directory, "output")
            with pinned_directory(directory, "output") as output:
                request = {"kind": "batch-family-export-request", "version": 1, "plan": plan, "decision": decision,
                           "workspace_identity": identity(directory_info(directory)),
                           "output_identity": identity(directory_info(output)), "basename": working.name}
            request["request_sha256"] = manifest_digest(request)
            with new_file(directory, "export-request.json") as stream:
                stream.write(encode_record(request))
    return {"workspace": str(workspace), "save_as": str(workspace / "output" / request["basename"]),
            "request_sha256": request["request_sha256"]}


def read_request(parent):
    request = read_request_file(parent, "export-request.json")
    validate_record(request, "batch-family-export-request", "request_sha256")
    plan = request["plan"]
    decision = request["decision"]
    validate_record(plan, "batch-family-type-derivation", "derivation_sha256")
    validate_record(decision, "batch-family-derivation-decision", "decision_sha256")
    if (request["workspace_identity"] != identity(directory_info(parent))
            or request["basename"] != parts(plan["source"]["path"])[-1]
            or decision["status"] != "kept_verified" or decision["derivation_sha256"] != plan["derivation_sha256"]):
        raise ValueError("The reserved export directory or basename changed.")
    return request


@contextmanager
def pinned_export(parent, workspace, request):
    with pinned_directory(parent, "output") as output, ExitStack() as leaves:
        if identity(directory_info(output)) != request["output_identity"]:
            raise ValueError("The reserved export output directory changed.")
        name = request["basename"]
        path = workspace / "output" / name
        if isinstance(output, WindowsDirectory):
            leaves.enter_context(windows.open_handle(path, directory=False))
        rfa = checked_file(output, name, child_info(output, name), MAX_FAMILY_BYTES,
                           {"bytes": 0, "deadline": monotonic() + MAX_SECONDS})
        budget = {"entries": 0, "bytes": 0, "deadline": monotonic() + MAX_SECONDS}
        start = verify_pair(output, rfa, None, budget)
        yield path, rfa
        if verify_pair(output, rfa, None, budget) != start:
            raise ValueError("The derived export changed during verification.")


def reopen_export(ui, raw_workspace, runtime):
    if not runtime:
        raise ValueError("The loaded pyRevit build is unavailable.")
    workspace = root_path(raw_workspace)
    with pinned_root(workspace) as parent:
        request = read_request(parent)
        plan = request["plan"]
        decision = request["decision"]
        with pinned_export(parent, workspace, request) as (path, rfa):
            with worker_document(ui, lambda app: app.OpenDocumentFile(os.fspath(path))) as document:
                if (not document.IsFamilyDocument or document.IsModified or document.IsWorkshared
                        or document.IsModelInCloud or normalized_path(document.PathName) != normalized_path(path)
                        or str(document.Application.VersionNumber) != "2026"
                        or str(document.Application.VersionBuild) != decision["revit_build"]):
                    raise ValueError("Revit did not reopen the exact exported family in the reviewed build.")
                family = document.OwnerFamily
                if (str(family.Name) != decision["family"] or str(family.FamilyCategory.Id.Value) != decision["category_id"]
                        or family_state(document) != decision["state"] or document.IsModified):
                    raise ValueError("The reopened export differs from the verified kept family.")
        receipt = {"kind": "batch-family-derived-rfa", "version": 1, "request_sha256": request["request_sha256"],
                   "derivation_sha256": plan["derivation_sha256"], "decision_sha256": decision["decision_sha256"],
                   "revit_build": decision["revit_build"], "state_sha256": manifest_digest(decision["state"]),
                   "workspace": str(workspace), "output_identity": request["output_identity"], "rfa": rfa,
                   "catalog": None, "source_path": plan["source"]["path"], "family": decision["family"],
                   "category_id": decision["category_id"], "types": [row["type"] for row in plan["rows"]],
                   "runtime": {"pyrevit_build": str(runtime), "engine": platform.python_implementation(),
                               "engine_version": platform.python_version()}}
        receipt["derived_sha256"] = manifest_digest(receipt)
        with new_file(parent, "export-receipt.json") as stream:
            stream.write(encode_record(receipt))
    return receipt


def collect_export(raw_workspace, staging):
    workspace = root_path(raw_workspace)
    with pinned_root(workspace) as parent:
        request = read_request(parent)
        plan = request["plan"]
        decision = request["decision"]
        receipt = read_request_file(parent, "export-receipt.json")
        validate_record(receipt, "batch-family-derived-rfa", "derived_sha256")
        if (receipt["request_sha256"] != request["request_sha256"] or receipt["workspace"] != str(workspace)
                or receipt["output_identity"] != request["output_identity"] or receipt["catalog"] is not None
                or receipt["derivation_sha256"] != plan["derivation_sha256"]
                or receipt["decision_sha256"] != decision["decision_sha256"]
                or receipt["revit_build"] != decision["revit_build"]
                or receipt["state_sha256"] != manifest_digest(decision["state"])
                or receipt["source_path"] != plan["source"]["path"] or receipt["family"] != decision["family"]
                or receipt["category_id"] != decision["category_id"]
                or receipt["types"] != [row["type"] for row in plan["rows"]]
                or receipt["runtime"]["engine"] != "CPython" or not receipt["runtime"]["pyrevit_build"]
                or staging["staging_sha256"] != plan["staging_sha256"]):
            raise ValueError("The derived receipt does not bind the reserved export and approved family.")
        source = plan["source"]
        live = discover(plan["root"])
        if (live["root_identity"] != plan["root_identity"]
                or [row for row in live["files"] if row["path"] == source["path"]] != [source]):
            raise ValueError("The reviewed original family or catalog changed.")
        with staged_pair(staging, source["path"]) as (_, pair), pinned_export(parent, workspace, request) as (_, rfa):
            if (pair["rfa"], pair["catalog"]) != (source["rfa"], source["catalog"]) or rfa != receipt["rfa"]:
                raise ValueError("The staged inputs or derived RFA changed after verification.")
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reserve = commands.add_parser("reserve")
    for name in ("derivation", "decision", "destination"):
        reserve.add_argument("--" + name, required=True)
    collect = commands.add_parser("collect")
    for name in ("workspace", "staging"):
        collect.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        result = (reserve_export(load_json(args.derivation), load_json(args.decision), args.destination)
                  if args.command == "reserve" else collect_export(args.workspace, load_json(args.staging)))
        print(encode_record(result).decode("ascii"))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        batch = __batchexec__
    except NameError:
        batch = False
    if batch:
        if os.name != "nt" or platform.python_implementation() != "CPython" or __models__:
            raise RuntimeError("Use the disposable Windows CPython worker with no model arguments.")
        from pyrevit import versionmgr
        reopen_export(__revit__, os.environ[EXPORT_ENV], versionmgr.get_loaded_build_version())
    else:
        sys.exit(main())
