# -*- coding: utf-8 -*-
"""Read-only worker for disposable pyRevit batch host qualification."""

from collections import Counter
import hashlib
import json
import os
import platform
import stat
import sys

MAX_FILE = 1024 ** 3
MAX_JSON = 128 * 1024
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
AUDITS = ("model_health.py", "param_qa.py")
HOST_STATUSES = ("audited", "incomplete", "rejected", "audit_failed", "close_failed",
                 "staged_changed", "not_run")


def linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & REPARSE_POINT)


def file_identity(info):
    if (linked(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or not info.st_ino or not 0 < info.st_size <= MAX_FILE):
        raise ValueError("staged input must be a unique ordinary file")
    return (info.st_dev, info.st_ino, info.st_size, getattr(info, "st_mtime_ns", info.st_mtime))


def staged_identity(path):
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        info = os.lstat(directory)
        if linked(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("staged path must use ordinary directories")
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return file_identity(os.lstat(path))


def staged_digest(path):
    identity = staged_identity(path)
    digest = hashlib.sha256()
    total = 0
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if file_identity(os.fstat(stream.fileno())) != identity:
            raise ValueError("staged input changed before hashing")
        while True:
            data = stream.read(1024 * 1024)
            if not data:
                break
            total += len(data)
            if total > MAX_FILE:
                raise ValueError("staged file too large")
            digest.update(data)
        if (total != identity[2] or file_identity(os.fstat(stream.fileno())) != identity
                or staged_identity(path) != identity):
            raise ValueError("staged input changed during hashing")
    return digest.hexdigest(), identity


def normalized_path(path):
    return os.path.normcase(os.path.abspath(path))


def summarize(health, parameters):
    if "error" in health or "error" in parameters:
        raise ValueError("audit returned an error")
    metrics = health["metrics"]
    cad_partial = health["cad_view_count"]["partial"]
    if not isinstance(metrics, list) or not metrics or type(cad_partial) is not bool:
        raise ValueError("invalid model health output")
    ids = [row["id"] for row in metrics]
    if len(set(ids)) != len(ids) or any(not row["id"] or row["status"] not in (
        "ok", "warn", "bad", "info"
    ) for row in metrics):
        raise ValueError("invalid model health metrics")
    categories = parameters["summary"]
    unknown = parameters["unknown_categories"]
    truncated = parameters["rows_truncated"]
    total = parameters["total_offenders"]
    if not isinstance(categories, list) or not categories or not isinstance(unknown, list):
        raise ValueError("invalid parameter QA output")
    if type(total) is not int or total < 0 or type(truncated) is not bool:
        raise ValueError("invalid parameter QA counts")
    failed = 0
    for row in categories:
        counts = (row["total"], row["passed"], row["failed"])
        if any(type(value) is not int or value < 0 for value in counts) or counts[0] != sum(counts[1:]):
            raise ValueError("invalid parameter QA category counts")
        failed += row["failed"]
    if failed != total:
        raise ValueError("parameter QA total does not match category counts")
    statuses = Counter(row["status"] for row in metrics)
    return {
        "health_metrics": len(metrics), "health_bad": statuses["bad"], "health_warn": statuses["warn"],
        "cad_coverage_partial": cad_partial, "parameter_offenders": total,
        "parameter_categories": len(categories), "parameter_unknown_categories": len(unknown),
        "parameter_details_truncated": truncated,
    }


def preflight(DB, path, year):
    info = DB.BasicFileInfo.Extract(path)
    try:
        if info.IsWorkshared or str(info.Format) != year:
            return None
    finally:
        info.Dispose()
    model_path = DB.ModelPathUtils.ConvertUserVisiblePathToModelPath(path)
    data = DB.TransmissionData.ReadTransmissionData(model_path)
    if data is not None:
        try:
            if data.IsTransmitted or list(data.GetAllExternalFileReferenceIds()):
                return None
        finally:
            data.Dispose()
    return model_path


def audit_source(script, doc, app, inputs):
    scope = {"doc": doc, "app": app, "inputs": inputs, "__name__": "__batch_audit__"}
    eval(compile(script, "<bundled audit>", "exec"), scope)
    return scope["result"]


def model_result(app, DB, path, model, year, scripts, audit=audit_source):
    row = {"id": model["id"], "sha256": model["sha256"], "status": "rejected"}
    document = None
    original = None
    try:
        original = staged_digest(path)
        if original[0] != model["sha256"]:
            row["status"] = "staged_changed"
            return row
        model_path = preflight(DB, path, year)
        if model_path is None:
            return row
        if staged_identity(path) != original[1]:
            row["status"] = "staged_changed"
            return row
        document = app.OpenDocumentFile(model_path, DB.OpenOptions())
        if (document.IsFamilyDocument or document.IsWorkshared or document.IsModelInCloud
                or document.IsModified or normalized_path(document.PathName) != normalized_path(path)):
            return row
        health = audit(scripts[0], document, app, {"max_ids": 1, "top_warnings": 5})
        parameters = audit(scripts[1], document, app, {"max_ids": 1, "max_rows": 1})
        if document.IsModified:
            return row
        summary = summarize(health, parameters)
        row["summary"] = summary
        row["status"] = "incomplete" if (
            summary["cad_coverage_partial"] or summary["parameter_unknown_categories"]
        ) else "audited"
    except Exception:
        row["status"] = "audit_failed"
    finally:
        if document is not None:
            try:
                if not document.Close(False) or list(app.Documents):
                    row["status"] = "close_failed"
            except Exception:
                row["status"] = "close_failed"
        elif list(app.Documents):
            row["status"] = "close_failed"
        try:
            if row["status"] != "close_failed" and (original is None or staged_digest(path) != original):
                row["status"] = "staged_changed"
        except (IOError, OSError, ValueError):
            if row["status"] != "close_failed":
                row["status"] = "staged_changed"
        if row["status"] not in ("audited", "incomplete"):
            row.pop("summary", None)
    return row


def read_manifest(directory):
    with open(os.path.join(directory, "host-manifest.json"), "rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("host manifest too large")
    return json.loads(raw)


def host_run(ui, DB, runtime, directory):
    app = ui.Application
    manifest = read_manifest(directory)
    year = manifest["revit_year"]
    if ui.ActiveUIDocument is not None or list(app.Documents) or str(app.VersionNumber) != year:
        raise ValueError("host must start with no open documents in the requested Revit year")
    if not runtime:
        raise ValueError("loaded pyRevit build could not be measured")
    models = manifest["models"]
    if not 1 <= len(models) <= 32 or [row["id"] for row in models] != list(range(1, len(models) + 1)):
        raise ValueError("invalid model manifest")
    scripts = []
    for name in AUDITS:
        with open(os.path.join(directory, name), "rb") as stream:
            data = stream.read(MAX_JSON + 1)
        if len(data) > MAX_JSON or hashlib.sha256(data).hexdigest() != manifest["audit_sha256"][name]:
            raise ValueError("bundled audit source changed")
        scripts.append(data)

    rows = []
    stop = False
    for model in models:
        path = os.path.join(directory, "model-%03d.rvt" % model["id"])
        row = {"id": model["id"], "sha256": model["sha256"], "status": "not_run"} if stop else model_result(
            app, DB, path, model, year, scripts,
        )
        rows.append(row)
        stop = stop or row["status"] == "close_failed"

    payload = {
        "kind": "revit-batch-host-receipt", "run_id": manifest["run_id"], "revit_year": year,
        "runtime": {"revit_build": str(app.VersionBuild), "pyrevit_build": str(runtime),
                    "engine": platform.python_implementation(), "engine_version": platform.python_version()},
        "rows": rows,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("ascii")
    if len(encoded) > MAX_JSON:
        raise ValueError("host receipt too large")
    receipt = os.path.join(directory, "host-receipt.json")
    if os.path.exists(receipt):
        raise ValueError("host receipt already exists")
    temp = receipt + ".tmp"
    with open(temp, "wb") as stream:
        stream.write(encoded)
    os.rename(temp, receipt)


if __name__ == "__main__":
    if not globals().get("__batchexec__"):
        raise RuntimeError("Run this worker only in a qualified pyRevit batch host.")
    import clr
    from pyrevit.api import DB
    from pyRevitLabs.Common import PyRevitBuildVersion
    from PyRevitLabs.PyRevit.Runtime import ScriptExecutor

    # The batch launcher can seed its session version from the clone on disk.
    runtime_assembly = clr.GetClrType(ScriptExecutor).Assembly
    host_run(__revit__, DB, PyRevitBuildVersion.FromAssembly(runtime_assembly),
             os.path.dirname(os.path.abspath(__file__)))
