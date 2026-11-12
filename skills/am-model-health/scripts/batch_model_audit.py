"""Stage a read-only Revit batch and verify its private audit receipt."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

from cad_file_inventory import link_or_reparse, local_path
from read_batch_models import AUDITS, HOST_STATUSES, MAX_FILE, MAX_JSON

BACKUP = re.compile(r"\.\d{4}\.rvt$", re.I)
SHA = re.compile(r"[0-9a-f]{64}\Z")
RUNTIME = re.compile(r"[a-zA-Z0-9._+ ():-]{1,100}\Z")
COUNTS = {"health_metrics", "health_bad", "health_warn", "parameter_offenders",
          "parameter_categories", "parameter_unknown_categories"}
FLAGS = {"cad_coverage_partial", "parameter_details_truncated"}


def checked_path(value, *, allow_new=False):
    path = local_path(value)
    for part in reversed(path.parents):
        info = part.lstat()
        if link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Select a local path with ordinary directories.")
    if not allow_new or path.exists() or path.is_symlink():
        if link_or_reparse(path.lstat()):
            raise ValueError("Reparse points and links are outside the audit scope.")
    return path


def regular_file(path):
    info = path.lstat()
    if link_or_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Use original regular files without alternate links.")
    return info


def digest(path, destination=None, *, max_file=MAX_FILE):
    initial = regular_file(path)
    flags = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    value = hashlib.sha256()
    total = 0
    with os.fdopen(os.open(path, flags), "rb") as stream:
        before = os.fstat(stream.fileno())
        if not (stat.S_ISREG(before.st_mode) and 0 < before.st_size <= max_file
                and os.path.samestat(initial, before)):
            limit = "1 GiB" if max_file == MAX_FILE else f"{max_file // (1024 * 1024)} MiB"
            raise ValueError(f"Audit inputs must be regular, nonempty files up to {limit}.")
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > max_file:
                limit = "1 GiB" if max_file == MAX_FILE else f"{max_file // (1024 * 1024)} MiB"
                raise ValueError(f"Input exceeds {limit}.")
            value.update(chunk)
            if destination is not None:
                destination.write(chunk)
        after = os.fstat(stream.fileno())
        current = regular_file(path)
        if not (total == before.st_size == after.st_size == current.st_size
                and before.st_mtime_ns == after.st_mtime_ns == current.st_mtime_ns
                and os.path.samestat(before, current)):
            raise ValueError("File changed while being read.")
    return value.hexdigest()


def read_json(path):
    regular_file(path)
    with path.open("rb") as stream:
        data = stream.read(MAX_JSON + 1)
    if len(data) > MAX_JSON:
        raise ValueError("Audit input exceeds the receipt limit.")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate receipt field.")
            value[key] = item
        return value

    return json.loads(data, object_pairs_hook=unique)


def staged_path(work, model_id):
    return work / f"model-{model_id:03}.rvt"


def prepare(directory, workspace, year):
    root = checked_path(directory)
    work = checked_path(workspace, allow_new=True)
    if not root.is_dir() or work == root or root in work.parents:
        raise ValueError("Use an input folder and a new workspace outside it.")
    sources = []
    for count, entry in enumerate(root.iterdir(), 1):
        if count > 10000:
            raise ValueError("Use a smaller source folder.")
        if entry.suffix.lower() == ".rvt" and not BACKUP.search(entry.name):
            info = regular_file(entry)
            sources.append((entry, info.st_size))
    sources.sort(key=lambda row: row[0].name.casefold())
    if not 1 <= len(sources) <= 32 or len({path.name.casefold() for path, _ in sources}) != len(sources):
        raise ValueError("Use 1 to 32 uniquely named RVT files.")
    total = sum(size for _, size in sources)
    if any(not 0 < size <= MAX_FILE for _, size in sources) or total > 4 * MAX_FILE:
        raise ValueError("The audit can stage up to 1 GiB per model and 4 GiB total.")
    if shutil.disk_usage(work.parent).free < total + MAX_FILE:
        raise ValueError("The workspace needs 1 GiB of free disk headroom after staging.")

    work.mkdir(mode=0o700)
    try:
        models = []
        for model_id, (source, size) in enumerate(sources, 1):
            before = regular_file(source)
            if before.st_size != size:
                raise ValueError("Input changed before staging.")
            with staged_path(work, model_id).open("xb") as destination:
                value = digest(source, destination)
                if destination.tell() != size:
                    raise ValueError("Input size changed during staging.")
            after = regular_file(source)
            if not os.path.samestat(before, after) or before.st_mtime_ns != after.st_mtime_ns:
                raise ValueError("Source identity changed during staging.")
            models.append({"id": model_id, "name": source.name, "sha256": value,
                           "device": before.st_dev, "inode": before.st_ino})

        scripts = Path(__file__).resolve().parent
        audit_sources = [scripts / AUDITS[0], scripts.parents[1] / "am-param-qa" / "scripts" / AUDITS[1]]
        audit_sha = {}
        for source in [*audit_sources, scripts / "read_batch_models.py"]:
            data = source.read_bytes()
            if len(data) > MAX_JSON:
                raise ValueError("Bundled audit source exceeds its size limit.")
            (work / source.name).write_bytes(data)
            audit_sha[source.name] = hashlib.sha256(data).hexdigest()
        manifest = {"run_id": uuid.uuid4().hex, "revit_year": year, "audit_sha256": audit_sha,
                    "models": [{key: model[key] for key in ("id", "sha256")} for model in models]}
        (work / "host-manifest.json").write_text(json.dumps(manifest), encoding="ascii")
    except BaseException:
        shutil.rmtree(work)
        raise
    return {"kind": "revit-batch-plan", "source_folder": str(root), "workspace": str(work),
            "run_id": manifest["run_id"], "revit_year": year, "models": models, "audit_sha256": audit_sha}


def model_plan(plan):
    models = plan["models"]
    if (plan["kind"] != "revit-batch-plan" or not 1 <= len(models) <= 32
            or [row["id"] for row in models] != list(range(1, len(models) + 1))
            or not re.fullmatch(r"[0-9a-f]{32}", plan["run_id"])
            or not re.fullmatch(r"20\d{2}", plan["revit_year"])):
        raise ValueError("Invalid batch plan.")
    for row in models:
        name = row["name"]
        if (type(row["id"]) is not int or not SHA.fullmatch(row["sha256"])
                or any(type(row[key]) is not int or row[key] < 0 for key in ("device", "inode"))
                or "/" in name or "\\" in name or ":" in name or not name.isprintable()
                or not name.lower().endswith(".rvt") or BACKUP.search(name)):
            raise ValueError("Invalid planned model identity.")
    if len({model["name"].casefold() for model in models}) != len(models):
        raise ValueError("Duplicate planned model identity.")
    return models


def valid_summary(summary):
    return (isinstance(summary, dict) and set(summary) == COUNTS | FLAGS
            and all(type(summary[key]) is int and summary[key] >= 0 for key in COUNTS)
            and all(type(summary[key]) is bool for key in FLAGS)
            and summary["health_metrics"] > 0 and summary["parameter_categories"] > 0
            and summary["health_bad"] + summary["health_warn"] <= summary["health_metrics"])


def read_receipt(work, plan, models):
    receipt = read_json(work / "host-receipt.json")
    if (receipt["kind"] != "revit-batch-host-receipt" or receipt["run_id"] != plan["run_id"]
            or receipt["revit_year"] != plan["revit_year"]):
        raise ValueError("Receipt does not belong to this batch.")
    runtime = receipt["runtime"]
    if set(runtime) != {"revit_build", "pyrevit_build", "engine", "engine_version"} or not all(
        isinstance(value, str) and RUNTIME.fullmatch(value) for value in runtime.values()
    ):
        raise ValueError("Receipt did not record the actual host runtime.")
    rows = receipt["rows"]
    if not isinstance(rows, list) or len(rows) != len(models):
        raise ValueError("Receipt does not account for every model.")
    for row, model in zip(rows, models):
        status = row["status"]
        expected_keys = {"id", "sha256", "status"}
        if status in ("audited", "incomplete"):
            expected_keys.add("summary")
            if not valid_summary(row["summary"]):
                raise ValueError("Receipt includes an invalid audit summary.")
            summary = row["summary"]
            partial = summary["cad_coverage_partial"] or summary["parameter_unknown_categories"] > 0
            if partial != (status == "incomplete"):
                raise ValueError("Receipt has inconsistent coverage.")
        if (set(row) != expected_keys or type(row["id"]) is not int or row["id"] != model["id"]
                or row["sha256"] != model["sha256"] or status not in HOST_STATUSES):
            raise ValueError("Receipt model identity or status is invalid.")
    return rows, runtime


def unchanged(path, expected, identity=None):
    try:
        before = regular_file(path)
        if identity is not None and (before.st_dev, before.st_ino) != identity:
            return False
        actual = digest(path)
        return actual == expected and os.path.samestat(before, regular_file(path))
    except (ValueError, OSError):
        return False


def collect(plan_path):
    path = checked_path(plan_path)
    plan = read_json(path)
    models = model_plan(plan)
    root = checked_path(plan["source_folder"])
    work = checked_path(plan["workspace"])
    if not root.is_dir() or not work.is_dir() or work in path.parents:
        raise ValueError("Keep the coordinator plan outside the host workspace.")
    result = []
    runtime = None
    failure = None
    try:
        rows, runtime = read_receipt(work, plan, models)
        pinned = plan["audit_sha256"]
        if set(pinned) != {*AUDITS, "read_batch_models.py"} or not all(
            SHA.fullmatch(sha) and unchanged(work / name, sha) for name, sha in pinned.items()
        ):
            raise ValueError("Bundled worker or audit sources changed.")
    except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError):
        failure = "The host receipt or staged audit sources could not be verified."
        rows = [{"status": "receipt_invalid"} for _ in models]

    for model, row in zip(models, rows):
        original_ok = unchanged(root / model["name"], model["sha256"], (model["device"], model["inode"]))
        staged_ok = unchanged(staged_path(work, model["id"]), model["sha256"])
        status = row["status"]
        if not staged_ok and status != "close_failed":
            status = "staged_changed"
        if not original_ok:
            status = "source_changed"
        final = {"id": model["id"], "model": model["name"], "status": status,
                 "original_unchanged": original_ok, "staged_unchanged": staged_ok}
        if status in ("audited", "incomplete"):
            final["summary"] = row["summary"]
        if row["status"] == "close_failed":
            final["host_close_failed"] = True
        result.append(final)
    complete = not failure and all(row["status"] == "audited" for row in result)
    return {"kind": "revit-batch-audit", "run_id": plan["run_id"],
            "status": "audited" if complete else "incomplete", "failure": failure,
            "runtime": runtime, "models": result}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("prepare")
    stage.add_argument("directory")
    stage.add_argument("--workspace", required=True)
    stage.add_argument("--revit", choices=("2026",), required=True)
    merge = commands.add_parser("collect")
    merge.add_argument("--plan", required=True)
    merge.add_argument("--host-exited", required=True, action="store_true")
    args = parser.parse_args(argv)
    try:
        report = (
            prepare(args.directory, args.workspace, args.revit)
            if args.command == "prepare" else collect(args.plan)
        )
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return int(report.get("status") == "incomplete")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        print(f"Batch audit failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
