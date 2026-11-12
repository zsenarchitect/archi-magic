"""Publish and search a private, local family library index."""

import argparse
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from io import BytesIO
import json
import os
import re
import stat
import sys
from time import monotonic
import unicodedata
from uuid import uuid4

from cad_file_inventory import link_or_reparse
from family_library_discovery import (
    MAX_FAMILIES, MAX_SECONDS, WindowsDirectory, checked_file, child_info, directory_info, discover, identity,
    manifest_digest, pinned_root, root_path,
)
from family_library_thumbnails import MAX_IMAGE_BYTES, png_record
from read_family_library import collect_worker, read_request_file, validate_parameter_names
from stage_family_library import HEADROOM, free_bytes, new_file

MAX_INDEX_BYTES = 32 * 1024 * 1024
MAX_ROWS = 5000
MAX_THUMBNAIL_BYTES = 128 * 1024 * 1024
THUMBNAIL_NAME = re.compile(r"thumbnail-[0-9a-f]{64}\.png")


def read_file(parent, name, limit):
    data = BytesIO()
    checked_file(parent, name, child_info(parent, name), limit,
                 {"bytes": 0, "deadline": monotonic() + MAX_SECONDS}, data)
    return data.getvalue()


def read_record(parent, name, kind):
    record = json.loads(read_file(parent, name, MAX_INDEX_BYTES))
    basis = {key: value for key, value in record.items() if key != "sha256"}
    if (record.get("kind") != kind or record.get("version") != 1
            or record.get("sha256") != manifest_digest(basis)):
        raise ValueError(f"The {kind} record is incomplete or changed.")
    return record


def replace_file(parent, name, data):
    temp = ".pending-" + uuid4().hex
    path_args = {} if isinstance(parent, WindowsDirectory) else {"src_dir_fd": parent, "dst_dir_fd": parent}
    source = parent.path / temp if isinstance(parent, WindowsDirectory) else temp
    target = parent.path / name if isinstance(parent, WindowsDirectory) else name
    created = False
    try:
        with new_file(parent, temp) as stream:
            created = True
            stream.write(data)
        os.replace(source, target, **path_args)
        if not isinstance(parent, WindowsDirectory):
            os.fsync(parent)
    finally:
        if created:
            try:
                os.unlink(source, **({} if isinstance(parent, WindowsDirectory) else {"dir_fd": parent}))
            except FileNotFoundError:
                pass


def write_record(parent, name, record):
    record["sha256"] = manifest_digest(record)
    data = json.dumps(record, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    if len(data) > MAX_INDEX_BYTES:
        raise ValueError("The family index exceeds 32 MiB.")
    replace_file(parent, name, data)


@contextmanager
def index_lock(path, parent):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    if isinstance(parent, WindowsDirectory):
        fd = os.open(path / "refresh.lock", flags, 0o600)
    else:
        fd = os.open("refresh.lock", flags | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(fd, "r+b", buffering=0) as stream:
        opened = os.fstat(fd)
        lock_info = (path / "refresh.lock").lstat() if isinstance(parent, WindowsDirectory) else child_info(parent, "refresh.lock")
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or link_or_reparse(lock_info) or not os.path.samestat(opened, lock_info)):
            raise ValueError("The family index lock must be a regular private file.")
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError("The family index is busy.") from error
        try:
            yield
        finally:
            if os.name == "nt":
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)


def prior_index(parent):
    try:
        return read_record(parent, "index.json", "family-library-index")
    except FileNotFoundError:
        return None


def begin(raw_index, manifest, parameter_names, revit_build, pyrevit_build):
    validate_parameter_names(parameter_names)
    basis = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if (manifest.get("kind") != "family-library-discovery" or manifest.get("version") != 1
            or manifest.get("manifest_sha256") != manifest_digest(basis)):
        raise ValueError("Use an intact family discovery manifest.")
    path, root = root_path(raw_index), root_path(manifest["root"])
    if path.is_relative_to(root) or root.is_relative_to(path):
        raise ValueError("Use a separate index directory outside the family library.")
    if any(not isinstance(build, str) or not build.strip() or len(build) > 100
           for build in (revit_build, pyrevit_build)):
        raise ValueError("Supply the expected Revit and pyRevit builds.")
    if os.name == "nt" and sys.version_info < (3, 13):
        raise ValueError("Use Python 3.13 or newer for a private Windows index.")
    with pinned_root(path.parent):
        path.mkdir(mode=0o700, exist_ok=True)
    ancestry = []
    with pinned_root(path, ancestry=ancestry) as parent:
        if os.name == "posix" and directory_info(parent).st_mode & 0o077:
            raise ValueError("Use an index directory accessible only to its owner.")
        if manifest["root_identity"] in ancestry:
            raise ValueError("The index resolves under the family library.")
        with index_lock(path, parent):
            try:
                previous = read_record(parent, "refresh.json", "family-library-refresh")
            except FileNotFoundError:
                previous = prior_index(parent)
            if previous is None:
                with os.scandir(path if isinstance(parent, WindowsDirectory) else parent) as entries:
                    if any(entry.name != "refresh.lock" for entry in entries):
                        raise ValueError("Create the family index in an empty private directory.")
            root_key = {"root": str(root), "root_identity": manifest["root_identity"]}
            if previous and any(previous[key] != value for key, value in root_key.items()):
                raise ValueError("This index belongs to a different library root.")
            refresh = {
                "kind": "family-library-refresh", "version": 1,
                **root_key, "index_identity": identity(directory_info(parent)),
                "number": previous["number"] + 1 if previous else 1,
                "token": uuid4().hex, "discovery_sha256": manifest["manifest_sha256"],
                "revit_build": revit_build, "pyrevit_build": pyrevit_build,
                "parameters": sorted(parameter_names),
            }
            write_record(parent, "refresh.json", refresh)
            return refresh


def copy_thumbnails(parent, worker, rows, seen):
    size = sum(seen.values())
    with pinned_root(root_path(worker)) as source:
        for row in rows:
            image = row.get("image")
            if not image:
                continue
            name = image["file"]
            if name in seen:
                continue
            data = read_file(source, name, MAX_IMAGE_BYTES)
            if png_record(data) != image:
                raise ValueError("A worker thumbnail changed before publication.")
            size += len(data)
            if size > MAX_THUMBNAIL_BYTES:
                raise ValueError("The refresh exceeds the thumbnail byte budget.")
            try:
                existing = read_file(parent, name, MAX_IMAGE_BYTES)
            except FileNotFoundError:
                if free_bytes(parent) - len(data) < HEADROOM:
                    raise ValueError("Index thumbnails would leave less than 1 GiB of free space.")
                replace_file(parent, name, data)
            else:
                if existing != data:
                    raise ValueError("An indexed thumbnail was changed.")
            seen[name] = len(data)


def remove_superseded_assets(parent, retained):
    windows = isinstance(parent, WindowsDirectory)
    with os.scandir(parent.path if windows else parent) as entries:
        for entry in entries:
            if entry.name not in retained and THUMBNAIL_NAME.fullmatch(entry.name):
                try:
                    if windows:
                        (parent.path / entry.name).unlink()
                    else:
                        os.unlink(entry.name, dir_fd=parent)
                except OSError as error:
                    print(f"Could not remove a superseded thumbnail: {error}", file=sys.stderr)


def publish(raw_index, token, workers):
    if len(workers) > MAX_FAMILIES:
        raise ValueError("Too many family workers for one refresh.")
    path = root_path(raw_index)
    with pinned_root(path) as parent, index_lock(path, parent):
        refresh = read_record(parent, "refresh.json", "family-library-refresh")
        old = prior_index(parent)
        if (refresh["token"] != token or refresh["index_identity"] != identity(directory_info(parent))
                or (old and old["number"] >= refresh["number"])):
            raise ValueError("This refresh no longer owns index publication.")
        live = discover(refresh["root"])
        if live["manifest_sha256"] != refresh["discovery_sha256"]:
            raise ValueError("Library discovery changed. Begin a new refresh.")
        files = {row["path"]: row for row in live["files"]}
        entries = {}
        seen = {}
        count = size = 0
        for worker in workers:
            result = collect_worker(worker)
            metadata = result["metadata"]
            relative = metadata["source_path"]
            source = files.get(relative)
            if (relative in entries or source is None or result["generation"] != token
                    or metadata["discovery_sha256"] != live["manifest_sha256"]
                    or metadata["requested_parameters"] != refresh["parameters"]
                    or metadata["revit_year"] != "2026" or metadata["revit_build"] != refresh["revit_build"]
                    or result["runtime"]["pyrevit_build"] != refresh["pyrevit_build"]
                    or metadata["rfa"] != source["rfa"] or metadata["catalog"] != source["catalog"]):
                raise ValueError("A worker does not belong to this refresh and source pair.")
            entry = {"status": "indexed", "metadata": metadata, "thumbnails": result["thumbnails"]}
            row_count = len(metadata["embedded_types"])
            entry_size = len(json.dumps({relative: entry}, ensure_ascii=True, separators=(",", ":")))
            if count + row_count > MAX_ROWS or size + entry_size > MAX_INDEX_BYTES - 2 * 1024 * 1024:
                entries[relative] = {"status": "index_limit"}
                continue
            count += row_count
            size += entry_size
            copy_thumbnails(parent, worker, result["thumbnails"], seen)
            entries[relative] = entry
        for relative in files.keys() - entries.keys():
            entries[relative] = {"status": "worker_unavailable"}
        if old and not live["complete"]:
            for relative in old["entries"].keys() - files.keys():
                entries[relative] = {"status": "source_unconfirmed"}
        if discover(refresh["root"])["manifest_sha256"] != live["manifest_sha256"]:
            raise ValueError("Library discovery changed during publication.")
        record = {
            "kind": "family-library-index", "version": 1,
            "root": live["root"], "root_identity": live["root_identity"],
            "number": refresh["number"], "token": token, "discovery_sha256": live["manifest_sha256"],
            "discovery_complete": live["complete"], "skipped": live["skipped"], "entries": entries,
        }
        write_record(parent, "index.json", record)
        try:
            remove_superseded_assets(parent, seen)
        except OSError as error:
            print(f"Could not scan for superseded thumbnails: {error}", file=sys.stderr)
        return record


def normalized(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def parameter_value(metadata, row, selector):
    key = normalized(selector)
    fields = [field for field in metadata["parameters"]
              if key in (normalized(field["id"]), normalized(field["shared_guid"] or ""))]
    if not fields:
        fields = [field for field in metadata["parameters"] if key == normalized(field["name"])]
    if len(fields) != 1:
        return None, len(fields) > 1
    field = fields[0]
    return {"definition": field, **row["values"][field["id"]]}, False


def matches_parameter_value(record, expected):
    if record["state"] != "value":
        return False
    value = record["value"]
    if isinstance(value, (float, int)):
        try:
            number = Decimal(expected)
        except InvalidOperation:
            return False
        return number.is_finite() and Decimal(str(value)) == number
    return normalized(value) == normalized(expected)


def search(raw_index, *, name="", category="", parameter=None, equals=None, limit=100):
    """Search one published index without opening Revit or the source files."""
    if (not 1 <= limit <= 500 or (parameter is not None and not normalized(parameter))
            or (equals is not None and parameter is None)):
        raise ValueError("Supply a parameter for value filters and a result limit from 1 to 500.")
    path = root_path(raw_index)
    matches, unavailable, ambiguous = [], [], set()
    total = 0
    with pinned_root(path) as parent, index_lock(path, parent):
        index = read_record(parent, "index.json", "family-library-index")
        for relative, entry in sorted(index["entries"].items()):
            if entry["status"] != "indexed":
                unavailable.append({"path": relative, "status": entry["status"]})
                continue
            metadata = entry["metadata"]
            if category and normalized(category) not in (normalized(metadata["category"]["name"]),
                                                          metadata["category"]["id"]):
                continue
            thumbnails = {row["type"]: row for row in entry["thumbnails"]}
            for row in metadata["embedded_types"]:
                if normalized(name) not in normalized(metadata["family"] + " " + row["name"]):
                    continue
                value = None
                if parameter is not None:
                    value, unclear = parameter_value(metadata, row, parameter)
                    if unclear:
                        ambiguous.add(relative)
                    if value is None:
                        continue
                    if equals is not None and not matches_parameter_value(value, equals):
                        continue
                total += 1
                if len(matches) >= limit:
                    continue
                thumbnail = thumbnails.get(row["name"], {"status": "not_requested"})
                image = thumbnail.get("image")
                if image:
                    try:
                        leaf = image["file"]
                        if not THUMBNAIL_NAME.fullmatch(leaf) or leaf != "thumbnail-" + image["sha256"] + ".png":
                            raise ValueError("Invalid thumbnail name.")
                        if png_record(read_file(parent, leaf, MAX_IMAGE_BYTES)) != image:
                            raise ValueError("Changed thumbnail.")
                    except (OSError, ValueError):
                        thumbnail = {"status": "asset_unavailable"}
                    else:
                        thumbnail = {"status": "available", "image": image, "path": str(path / leaf)}
                matches.append({
                    "family": metadata["family"], "type": row["name"], "category": metadata["category"],
                    "path": relative, "source_pair": {"rfa": metadata["rfa"], "catalog": metadata["catalog"]},
                    "thumbnail": thumbnail, "parameter": value, "catalog_coverage": metadata["catalog_coverage"],
                    "embedded_type_coverage": metadata["embedded_type_coverage"],
                })
    return {
        "generation": index["token"], "discovery_sha256": index["discovery_sha256"],
        "discovery_complete": index["discovery_complete"],
        "matches": matches, "total_matches": total, "unavailable": unavailable, "skipped": index["skipped"],
        "ambiguous_parameter_paths": sorted(ambiguous),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("begin", help="Reserve a refresh generation for a discovery manifest")
    start.add_argument("index")
    start.add_argument("manifest")
    start.add_argument("--parameter", action="append", default=[])
    start.add_argument("--revit-build", required=True)
    start.add_argument("--pyrevit-build", required=True)
    finish = commands.add_parser("publish", help="Publish verified worker receipts")
    finish.add_argument("index")
    finish.add_argument("generation")
    finish.add_argument("--worker", action="append", default=[])
    find = commands.add_parser("search", help="Search the last published generation")
    find.add_argument("index")
    find.add_argument("--name", default="")
    find.add_argument("--category", default="")
    find.add_argument("--parameter")
    find.add_argument("--equals")
    find.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    try:
        if args.command == "begin":
            manifest = root_path(args.manifest)
            with pinned_root(manifest.parent) as parent:
                data = read_request_file(parent, manifest.name)
            result = begin(args.index, data, args.parameter, args.revit_build, args.pyrevit_build)
        elif args.command == "publish":
            result = publish(args.index, args.generation, args.worker)
        else:
            result = search(args.index, name=args.name, category=args.category, parameter=args.parameter,
                            equals=args.equals, limit=args.limit)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
