#! python3
"""Reserve and seal a private family transfer export after a verified user Keep."""

import argparse
from contextlib import contextmanager
import os
import platform
import sys
from uuid import uuid4

from batch_family_export import pinned_export
from batch_family_type_preview import validate_record
from family_library_discovery import (
    MAX_FAMILY_BYTES, directory_info, identity, manifest_digest, pinned_directory, pinned_root, root_path,
)
from family_parameter_transfer_preview import reviewed_snapshot
from family_placement_preview import load_json
from probe_family_parameter_transfer import pinned_copy
from read_batch_models import normalized_path
from read_family_library import encode_record, read_request_file, worker_document
from read_family_parameter_transfer import snapshot
from stage_family_library import HEADROOM, free_bytes, make_directory, new_file, parts
from verify_family_parameter_transfer import read_audit, verify_decision

EXPORT_ENV = "ARCHI_MAGIC_TRANSFER_EXPORT_DIR"
REQUEST_FILE = "export-request.json"
RECEIPT_FILE = "export-receipt.json"


def reserve(transfer, before, trial, after, modify, fresh, raw_destination):
    decision = verify_decision(transfer, before, trial, after, modify, fresh)
    if decision["status"] != "family_transfer_kept":
        raise ValueError("A verified native Keep is required to reserve an export.")
    audit, _ = read_audit(fresh.get("response", fresh))
    target = reviewed_snapshot(transfer["target_read"], "target", transfer["request"]["target_sha256"])
    if os.name == "nt" and sys.version_info < (3, 13):
        raise ValueError("Use Python 3.13 or newer to reserve a private Windows export.")
    destination = root_path(raw_destination)
    name = "family-transfer-export-" + uuid4().hex
    workspace = destination / name
    ancestry = []
    with pinned_copy(transfer["working_copy"]) as working, pinned_root(destination, ancestry=ancestry) as parent:
        if transfer["working_copy"]["workspace_identity"] in ancestry:
            raise ValueError("Reserve the export outside the reviewed working-copy directory.")
        if free_bytes(parent) - MAX_FAMILY_BYTES < HEADROOM:
            raise ValueError("The reserved export would leave less than 1 GiB free.")
        make_directory(parent, name)
        with pinned_directory(parent, name) as directory:
            make_directory(directory, "output")
            with pinned_directory(directory, "output") as output:
                request = {"kind": "family-transfer-export-request", "version": 1,
                           "workspace_identity": identity(directory_info(directory)),
                           "output_identity": identity(directory_info(output)),
                           "basename": working.name, "decision_sha256": decision["decision_sha256"],
                           "preview_sha256": decision["preview_sha256"], "target_sha256": target["read_sha256"],
                           "project": target["project"], "loaded_family": target["loaded_family"],
                           "family": target["family"], "category_id": target["category_id"],
                           "revit_build": target["revit_build"], "state_sha256": manifest_digest(audit["state"])}
            request["request_sha256"] = manifest_digest(request)
            with new_file(directory, REQUEST_FILE) as stream:
                stream.write(encode_record(request))
    return {"workspace": str(workspace), "save_as": str(workspace / "output" / request["basename"]),
            "request_sha256": request["request_sha256"]}


def read_request(parent, expected_sha256):
    request = read_request_file(parent, REQUEST_FILE)
    validate_record(request, "family-transfer-export-request", "request_sha256")
    basename, = parts(request["basename"])
    if (basename != request["basename"] or request["request_sha256"] != expected_sha256
            or request["workspace_identity"] != identity(directory_info(parent))):
        raise ValueError("The export reservation differs from the reviewed request.")
    return request


def export_receipt(workspace, request, rfa, runtime):
    if (runtime["engine"] != "CPython" or not runtime["pyrevit_build"] or not runtime["engine_version"]):
        raise ValueError("Use the qualified disposable CPython worker with a reported pyRevit build.")
    retained = ("request_sha256", "output_identity", "decision_sha256", "preview_sha256", "target_sha256",
                "project", "loaded_family", "family", "category_id", "revit_build", "state_sha256")
    receipt = {key: request[key] for key in retained}
    receipt.update(kind="family-transfer-derived-rfa", version=1, workspace=str(workspace), rfa=rfa, runtime=runtime)
    return dict(receipt, derived_sha256=manifest_digest(receipt))


def reopen(ui, DB, raw_workspace, request_sha256, pyrevit_build):
    if not pyrevit_build or platform.python_implementation() != "CPython":
        raise ValueError("The loaded CPython pyRevit runtime is unavailable.")
    workspace = root_path(raw_workspace)
    with pinned_root(workspace) as parent:
        request = read_request(parent, request_sha256)
        with pinned_export(parent, workspace, request) as (path, rfa):
            with worker_document(ui, lambda app: app.OpenDocumentFile(os.fspath(path))) as document:
                if normalized_path(document.PathName) != normalized_path(path):
                    raise ValueError("Revit did not reopen the exact exported family.")
                read = snapshot(document, DB)
                if (any(read[key] != request[key] for key in ("family", "category_id", "revit_build"))
                        or read["state"]["coverage"] != {"complete": True, "issues": []}
                        or manifest_digest(read["state"]) != request["state_sha256"]):
                    raise ValueError("The reopened export differs from the independently verified kept family.")
        runtime = {"pyrevit_build": str(pyrevit_build), "engine": platform.python_implementation(),
                   "engine_version": platform.python_version()}
        receipt = export_receipt(workspace, request, rfa, runtime)
        with new_file(parent, RECEIPT_FILE) as stream:
            stream.write(encode_record(receipt))
    return receipt


@contextmanager
def verified_export(raw_workspace, request_sha256, *, expected_derived_sha256=None):
    workspace = root_path(raw_workspace)
    with pinned_root(workspace) as parent:
        request = read_request(parent, request_sha256)
        receipt = read_request_file(parent, RECEIPT_FILE)
        if expected_derived_sha256 is not None and receipt["derived_sha256"] != expected_derived_sha256:
            raise ValueError("The exported family receipt differs from the reviewed reload input.")
        with pinned_export(parent, workspace, request) as (path, rfa):
            if receipt != export_receipt(workspace, request, rfa, receipt["runtime"]):
                raise ValueError("The export receipt does not match the reservation and current file bytes.")
            yield path, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reserve_command = commands.add_parser("reserve")
    for name in ("transfer", "before", "trial", "after", "modify", "fresh", "destination"):
        reserve_command.add_argument("--" + name, required=True)
    collect_command = commands.add_parser("collect")
    collect_command.add_argument("--workspace", required=True)
    collect_command.add_argument("--request-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "reserve":
            records = [load_json(getattr(args, name)) for name in ("transfer", "before", "trial", "after", "modify", "fresh")]
            receipt = reserve(*records, args.destination)
        else:
            with verified_export(args.workspace, args.request_sha256) as (_, receipt):
                pass
        print(encode_record(receipt).decode("ascii"))
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
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
        from pyrevit.api import DB

        reopen(__revit__, DB, os.environ[EXPORT_ENV], os.environ[EXPORT_ENV + "_REQUEST_SHA256"],
               versionmgr.get_loaded_build_version())
    else:
        sys.exit(main())
