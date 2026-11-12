"""Stage verified working copies for a Revit family thumbnail batch."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import stat
import sys
import tempfile
import unicodedata

from batch_model_audit import SHA, checked_path, digest, read_json, regular_file
from cad_file_inventory import RESERVED

MIB = 1024 * 1024
MAX_ENTRIES = 250
MAX_FAMILIES = 25
MAX_FILE = 128 * MIB
MAX_TOTAL = 512 * MIB
HEADROOM = 256 * MIB
BACKUP_CANDIDATE = re.compile(r"\.\d{4}\.rfa$", re.IGNORECASE)
MANIFEST = "family-preview-manifest.json"


def local_directory(raw):
    path = checked_path(raw)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ValueError("Select an ordinary local folder.")
    path = path.resolve(strict=True)
    if os.name == "nt":
        from ctypes import windll
        if windll.kernel32.GetDriveTypeW(str(path.anchor)) != 3:
            raise ValueError("Select a local fixed drive.")
    return path


def name_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def valid_name(name):
    return (isinstance(name, str) and name.isprintable() and len(name) <= 200
            and name == PureWindowsPath(name).name and not RESERVED.match(name)
            and not any(char in name for char in '<>:"/\\|?*')
            and len(name) > 4 and name.lower().endswith(".rfa"))


def entries(root):
    result = {}
    for number, path in enumerate(root.iterdir(), 1):
        if number > MAX_ENTRIES:
            raise ValueError("The folder exceeds 250 entries. Review a smaller folder.")
        if path.suffix.lower() != ".rfa":
            continue
        key = name_key(path.name)
        if key in result:
            raise ValueError("Family names collide on a case-insensitive filesystem.")
        result[key] = path
    return result


def inventory(root):
    candidates = []
    for path in sorted(entries(root).values(), key=lambda item: name_key(item.name)):
        try:
            if not valid_name(path.name):
                raise ValueError("The filename is not portable to Revit on Windows.")
            info = regular_file(path)
            if not 0 < info.st_size <= MAX_FILE:
                raise ValueError("The family file must be nonempty and at most 128 MiB.")
            candidates.append({"name": path.name, "bytes": info.st_size,
                               "backup_candidate": bool(BACKUP_CANDIDATE.search(path.name)),
                               "status": "available"})
        except (OSError, ValueError) as error:
            candidates.append({"name": path.name, "status": "refused", "reason": str(error)})
    return {"kind": "family-preview-inventory", "families": candidates}


def check_separation(*roots):
    for index, first in enumerate(roots):
        for second in roots[index + 1:]:
            if first.samefile(second) or first in second.parents or second in first.parents:
                raise ValueError("Use separate source, staging and output folders.")


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def family_rows(root, output, selected):
    if (not 1 <= len(selected) <= MAX_FAMILIES or not all(valid_name(name) for name in selected)
            or len({name_key(name) for name in selected}) != len(selected)):
        raise ValueError("Select one to 25 uniquely named RFA files by their exact names.")
    source_entries = entries(root)
    output_entries = entries(output)
    rows = []
    for name in selected:
        key = name_key(name)
        source = source_entries.get(key)
        if source is None or source.name != name:
            raise ValueError(f"Family not found by its exact name: {name}")
        if key in output_entries:
            raise ValueError(f"The output family already exists: {name}")
        details = identity(regular_file(source))
        if not 0 < details[2] <= MAX_FILE:
            raise ValueError(f"The family file must be nonempty and at most 128 MiB: {name}")
        rows.append({"name": name, "source_identity": details})
    total = sum(row["source_identity"][2] for row in rows)
    if total > MAX_TOTAL:
        raise ValueError("The selected families exceed 512 MiB.")
    return rows, total


def check_space(staging_parent, output, total):
    if staging_parent.stat().st_dev == output.stat().st_dev:
        checks = [(staging_parent, 2 * total)]
    else:
        checks = [(staging_parent, total), (output, total)]
    if any(shutil.disk_usage(folder).free < required + HEADROOM for folder, required in checks):
        raise ValueError("Staging and output need enough room for the batch plus 256 MiB of headroom.")


def copy_family(source, destination, row):
    expected = row["source_identity"]
    if identity(regular_file(source)) != expected:
        raise ValueError(f"The source family changed before staging: {source.name}")
    before = digest(source, max_file=MAX_FILE)
    with destination.open("xb") as target:
        copied = digest(source, target, max_file=MAX_FILE)
    after = digest(source, max_file=MAX_FILE)
    staged = digest(destination, max_file=MAX_FILE)
    if (before != copied or after != copied or staged != copied
            or identity(regular_file(source)) != expected):
        raise ValueError(f"The source and staged family could not be reconciled: {source.name}")
    row["sha256"] = copied
    row["staged_identity"] = identity(regular_file(destination))


def stage(root, staging_parent, output, selected):
    check_separation(root, staging_parent, output)
    rows, total = family_rows(root, output, selected)
    check_space(staging_parent, output, total)
    workspace = Path(tempfile.mkdtemp(prefix="family-previews-", dir=staging_parent))
    workspace_identity = workspace.lstat()
    try:
        for row in rows:
            name = row["name"]
            copy_family(root / name, workspace / name, row)
        manifest = {"kind": "family-preview-stage", "source_folder": str(root), "output_folder": str(output),
                    "workspace": str(workspace), "families": rows}
        with (workspace / MANIFEST).open("x", encoding="ascii") as stream:
            json.dump(manifest, stream, ensure_ascii=True, indent=2)
    except BaseException:
        current = workspace.lstat()
        if stat.S_ISDIR(current.st_mode) and os.path.samestat(workspace_identity, current):
            shutil.rmtree(workspace)
        raise
    return {"kind": manifest["kind"], "manifest": str(workspace / MANIFEST), "count": len(rows)}


def check_file(path, expected, sha):
    before = identity(regular_file(path))
    if before != expected or digest(path, max_file=MAX_FILE) != sha or identity(regular_file(path)) != before:
        raise ValueError("File bytes or identity changed.")


def verify(manifest_path, source_only, family=None):
    path = checked_path(manifest_path)
    record = read_json(path)
    root = local_directory(record["source_folder"])
    work = local_directory(record["workspace"])
    output = local_directory(record["output_folder"])
    check_separation(root, work, output)
    rows = record["families"]
    if (record["kind"] != "family-preview-stage" or path.resolve() != work / MANIFEST
            or not isinstance(rows, list) or not 1 <= len(rows) <= MAX_FAMILIES):
        raise ValueError("Invalid family preview staging manifest.")
    names = [row["name"] for row in rows]
    if not all(valid_name(name) for name in names) or len({name_key(name) for name in names}) != len(names):
        raise ValueError("Invalid family names in the staging manifest.")
    if family is not None:
        rows = [row for row in rows if row["name"] == family]
        if not rows:
            raise ValueError("That exact family name is absent from the manifest.")
    output_entries = {} if source_only else entries(output)
    results = []
    for row in rows:
        name = row["name"]
        sha = row["sha256"]
        if (not isinstance(sha, str) or not SHA.fullmatch(sha)
                or any(not isinstance(row[field], list) or len(row[field]) != 4
                       or any(type(value) is not int for value in row[field])
                       for field in ("source_identity", "staged_identity"))):
            raise ValueError("Invalid family fingerprints in the staging manifest.")
        result = {"name": name, "source": "verified"}
        try:
            check_file(root / name, row["source_identity"], sha)
        except (ValueError, OSError):
            result["source"] = "changed or unavailable"
        if not source_only:
            result["staged"] = "verified"
            try:
                check_file(work / name, row["staged_identity"], sha)
            except (ValueError, OSError):
                result["staged"] = "changed or unavailable"
            result["output"] = "conflict" if name_key(name) in output_entries else "available"
        results.append(result)
    ready = all(row["source"] == "verified" and row.get("staged", "verified") == "verified"
                and row.get("output", "available") == "available" for row in results)
    report = {"kind": "family-preview-stage-verification", "status": "verified" if ready else "incomplete",
              "source_only": source_only, "families": results}
    if ready and family is not None and not source_only:
        path_key = os.path.normcase(str(work / family)).encode("utf-8")
        report["revit_inputs"] = {"staged_path_sha256": hashlib.sha256(path_key).hexdigest(),
                                  "staged_file_sha256": rows[0]["sha256"]}
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("inventory")
    scan.add_argument("source")
    prepare = commands.add_parser("stage")
    prepare.add_argument("source")
    prepare.add_argument("--staging-parent", required=True)
    prepare.add_argument("--output", required=True)
    prepare.add_argument("--family", action="append", required=True)
    check = commands.add_parser("verify")
    check.add_argument("manifest")
    check.add_argument("--source-only", action="store_true")
    check.add_argument("--family")
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            report = inventory(local_directory(args.source))
        elif args.command == "stage":
            report = stage(local_directory(args.source), local_directory(args.staging_parent),
                           local_directory(args.output), args.family)
        else:
            report = verify(args.manifest, args.source_only, args.family)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return int(report.get("status") == "incomplete")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        print(f"Family staging stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
