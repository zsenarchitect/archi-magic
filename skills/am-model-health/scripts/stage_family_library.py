"""Stage reviewed RFA and catalog pairs in a new private workspace."""

import argparse
from collections import defaultdict
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
from time import monotonic
from uuid import uuid4

from family_library_discovery import (
    MAX_CATALOG_BYTES, MAX_DEPTH, MAX_FAMILIES, MAX_FAMILY_BYTES, MAX_SECONDS, MAX_TOTAL_BYTES, WindowsDirectory,
    child_info, classify_entries, directory_info, directory_stamp, identity, manifest_digest,
    pinned_directory, pinned_root, record_pair, root_path,
)

MAX_MANIFEST = 8 * 1024 * 1024
HEADROOM = 1024 * 1024 * 1024


def parts(relative):
    if not isinstance(relative, str):
        raise ValueError("Expected an exact root-relative family path.")
    components = relative.split("/")
    if (not 1 <= len(components) <= MAX_DEPTH + 1
            or any(name in ("", ".", "..") or any(char in name for char in ("\\", ":", "\x00"))
                   for name in components)
            or Path(components[-1]).suffix.casefold() != ".rfa"):
        raise ValueError("Expected an exact root-relative RFA path.")
    return components


def check_file_record(record, basename, limit):
    digest = record.get("sha256")
    if (set(record) != {"basename", "bytes", "sha256"} or record.get("basename") != basename
            or type(record.get("bytes")) is not int or not 0 < record["bytes"] <= limit
            or not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)):
        raise ValueError("A discovery file record is malformed.")


def selected_rows(manifest, selected):
    basis = {name: value for name, value in manifest.items() if name != "manifest_sha256"}
    if (manifest.get("kind") != "family-library-discovery" or type(manifest.get("version")) is not int
            or manifest["version"] != 1 or manifest.get("manifest_sha256") != manifest_digest(basis)
            or not isinstance(manifest.get("files"), list) or len(manifest["files"]) > MAX_FAMILIES
            or not selected or len(set(selected)) != len(selected)):
        raise ValueError("Use one intact discovery manifest and distinct reviewed family paths.")
    lookup = {}
    for row in manifest["files"]:
        relative = row["path"]
        components = parts(relative)
        if relative in lookup or len(row["ancestors"]) != len(components):
            raise ValueError("A discovery path has duplicate or missing ancestry.")
        check_file_record(row["rfa"], components[-1], MAX_FAMILY_BYTES)
        catalog = row["catalog"]
        if catalog is not None:
            name = catalog["basename"]
            if not isinstance(name, str) or name.casefold() != (Path(components[-1]).stem + ".txt").casefold():
                raise ValueError("The companion catalog basename does not match the family.")
            check_file_record(catalog, name, MAX_CATALOG_BYTES)
        lookup[relative] = row
    if any(relative not in lookup for relative in selected):
        raise ValueError("A reviewed path is unavailable in the discovery manifest.")
    return [lookup[relative] for relative in selected]


def make_directory(parent, name):
    if isinstance(parent, WindowsDirectory):
        (parent.path / name).mkdir(mode=0o700)
    else:
        os.mkdir(name, mode=0o700, dir_fd=parent)


@contextmanager
def new_file(parent, name):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if isinstance(parent, WindowsDirectory):
        descriptor = os.open(parent.path / name, flags, 0o600)
    else:
        descriptor = os.open(name, flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(descriptor, "wb") as stream:
        yield stream
        stream.flush()
        os.fsync(stream.fileno())


def free_bytes(parent):
    if isinstance(parent, WindowsDirectory):
        return shutil.disk_usage(parent.path).free
    info = os.fstatvfs(parent)
    return info.f_bavail * info.f_frsize


def stage_folder(source, workspace, directory, rows, budget, verify_budget):
    with ExitStack() as stack:
        parent = source
        ancestry = [identity(directory_info(parent))]
        for name in directory:
            parent = stack.enter_context(pinned_directory(parent, name))
            ancestry.append(identity(directory_info(parent)))
        start = directory_stamp(directory_info(parent))
        entries = classify_entries(parent, budget)
        staged = []
        for row in rows:
            name = row["rfa"]["basename"]
            if row["ancestors"] != ancestry or len(entries.get(name.casefold(), [])) != 1:
                raise ValueError("A reviewed family path or its ancestry changed after discovery.")
            enumerated_name, inode = entries[name.casefold()][0]
            if enumerated_name != name:
                raise ValueError("A reviewed family basename changed after discovery.")
            info = child_info(parent, name)
            if inode is not None and inode != info.st_ino:
                raise ValueError("A reviewed family changed during staging.")
            catalogs = entries.get((Path(name).stem + ".txt").casefold(), [])
            expected_catalog = row["catalog"]["basename"] if row["catalog"] else None
            if len(catalogs) > 1 or (catalogs[0][0] if catalogs else None) != expected_catalog:
                raise ValueError("A companion catalog appeared, disappeared, or changed its name.")
            pair_dir = manifest_digest(row)
            make_directory(workspace, pair_dir)
            with pinned_directory(workspace, pair_dir) as destination:
                rfa, catalog = record_pair(parent, name, info, catalogs, budget,
                                           copy_to=lambda leaf: new_file(destination, leaf))
                if (rfa, catalog) != (row["rfa"], row["catalog"]):
                    raise ValueError("A family or companion catalog changed after discovery.")
                copy_stamp = directory_stamp(directory_info(destination))
                entries_after_copy = classify_entries(destination, verify_budget)
                expected_names = {name} if expected_catalog is None else {name, expected_catalog}
                actual_names = {leaf for group in entries_after_copy.values() for leaf, _ in group}
                if actual_names != expected_names:
                    raise ValueError("The staging directory contains unexpected files.")
                copies = entries_after_copy.get(expected_catalog.casefold(), []) if expected_catalog else []
                copy_info = child_info(destination, name)
                copy_inode = entries_after_copy[name.casefold()][0][1]
                if copy_inode is not None and copy_inode != copy_info.st_ino:
                    raise ValueError("A staged family was replaced during verification.")
                copied = record_pair(destination, name, copy_info, copies, verify_budget)
                if copied != (rfa, catalog) or directory_stamp(directory_info(destination)) != copy_stamp:
                    raise ValueError("The staged file bytes differ from the reviewed sources.")
                pair_identity = identity(directory_info(destination))
            staged.append({"source_path": row["path"], "directory": pair_dir, "directory_identity": pair_identity,
                           "rfa": rfa, "catalog": catalog})
        if directory_stamp(directory_info(parent)) != start:
            raise ValueError("A source directory changed during staging.")
    return staged


def stage(manifest, selected, raw_destination):
    if os.name == "nt" and sys.version_info < (3, 13):
        raise ValueError("Use Python 3.13 or newer for a private Windows staging workspace.")
    rows = selected_rows(manifest, selected)
    total = sum(row["rfa"]["bytes"] + (row["catalog"]["bytes"] if row["catalog"] else 0) for row in rows)
    if total > MAX_TOTAL_BYTES:
        raise ValueError("The selected sources exceed the staging byte limit.")
    source_root = root_path(manifest["root"])
    destination_root = root_path(raw_destination)
    if destination_root.is_relative_to(source_root):
        raise ValueError("Select an ordinary staging parent outside the family library.")

    grouped = defaultdict(list)
    for row in rows:
        grouped[tuple(parts(row["path"])[:-1])].append(row)
    budget = {"entries": 0, "bytes": 0, "deadline": monotonic() + MAX_SECONDS}
    verify_budget = {"entries": 0, "bytes": 0, "deadline": budget["deadline"]}
    name = "family-library-" + uuid4().hex
    output = destination_root / name
    ancestry = []
    created = False
    try:
        with pinned_root(destination_root, ancestry=ancestry) as destination:
            if manifest["root_identity"] in ancestry:
                raise ValueError("The staging parent resolves under the family library.")
            if free_bytes(destination) - total < HEADROOM:
                raise ValueError("Staging would leave less than 1 GiB of free space.")
            make_directory(destination, name)
            created = True
            with pinned_directory(destination, name) as workspace, pinned_root(source_root) as source:
                if identity(directory_info(source)) != manifest["root_identity"]:
                    raise ValueError("The library root changed after discovery.")
                workspace_identity = identity(directory_info(workspace))
                pairs = []
                for directory, directory_rows in grouped.items():
                    pairs.extend(stage_folder(source, workspace, directory, directory_rows, budget, verify_budget))
    except (OSError, ValueError) as error:
        if created:
            raise ValueError(f"{error} The unpublished workspace remains at {output}.") from error
        raise
    return {
        "kind": "family-library-staging", "version": 1, "workspace": str(output),
        "workspace_identity": workspace_identity,
        "discovery_sha256": manifest["manifest_sha256"], "pairs": pairs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("destination_parent")
    parser.add_argument("--path", action="append", required=True, help="Exact reviewed RFA path from the manifest")
    args = parser.parse_args()
    try:
        with open(args.manifest, "rb") as stream:
            raw = stream.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            raise ValueError("The discovery manifest exceeds 8 MiB.")
        result = stage(json.loads(raw), args.path, args.destination_parent)
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
