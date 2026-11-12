"""Discover exact Revit family and companion catalog files under an ordinary local folder."""

import argparse
from collections import defaultdict
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from time import monotonic

from cad_file_inventory import link_or_reparse, local_path

MAX_ENTRIES = 10_000
MAX_DEPTH = 12
MAX_FAMILIES = 500
MAX_FAMILY_BYTES = 256 * 1024 * 1024
MAX_CATALOG_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_SECONDS = 120
CHUNK_BYTES = 1024 * 1024


class ScanLimit(ValueError):
    """The discovery budget cannot support a complete manifest."""


def identity(info):
    return [info.st_dev, info.st_ino]


def directory_stamp(info):
    return (*identity(info), info.st_mtime_ns, info.st_ctime_ns)


def file_stamp(info):
    return (*directory_stamp(info), info.st_mode, info.st_size, info.st_nlink)


def file_record(name, size, digest):
    return {"basename": name, "bytes": size, "sha256": digest}


def directory_flags():
    if (os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
            or os.scandir not in os.supports_fd or os.open not in os.supports_dir_fd):
        raise ValueError("This discovery path requires qualified no-follow directory handles.")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def pinned_directory(parent, name, expected=None):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or link_or_reparse(before):
        raise ValueError("The path is not an ordinary directory.")
    if expected is not None and identity(before) != identity(expected):
        raise ValueError("A directory changed after enumeration.")
    fd = os.open(name, directory_flags(), dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if identity(opened) != identity(before):
            raise ValueError("A directory was replaced before it could be read.")
        yield fd
        final = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if identity(final) != identity(opened) or link_or_reparse(final):
            raise ValueError("A directory changed during discovery.")
    finally:
        os.close(fd)


@contextmanager
def pinned_root(path):
    with ExitStack() as opened:
        parent = opened.enter_context(pinned_directory(None, path.anchor))
        for name in path.parts[1:]:
            parent = opened.enter_context(pinned_directory(parent, name))
        yield parent


def ordinary_file(info, cap):
    return (stat.S_ISREG(info.st_mode) and not link_or_reparse(info)
            and info.st_nlink == 1 and 0 < info.st_size <= cap)


def checked_file(parent, name, enumerated, cap, budget):
    if not ordinary_file(enumerated, cap):
        raise ValueError("Expected a bounded regular file without links.")
    budget["bytes"] += enumerated.st_size
    if budget["bytes"] > MAX_TOTAL_BYTES:
        raise ScanLimit("The discovery byte budget was exceeded.")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(name, flags, dir_fd=parent), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if file_stamp(opened) != file_stamp(enumerated) or not ordinary_file(opened, cap):
            raise ValueError("A source file changed before it was opened.")
        size = 0
        digest = hashlib.sha256()
        while True:
            if monotonic() > budget["deadline"]:
                raise ScanLimit("The discovery time budget was exceeded.")
            data = stream.read(min(CHUNK_BYTES, opened.st_size - size + 1))
            if not data:
                break
            size += len(data)
            if size > opened.st_size:
                raise ValueError("A source file grew while it was being read.")
            digest.update(data)
        final = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if size != opened.st_size or file_stamp(opened) != file_stamp(os.fstat(stream.fileno())) or (
                file_stamp(opened) != file_stamp(final)):
            raise ValueError("A source file changed while it was being read.")
    return file_record(name, size, digest.hexdigest())


def classify_entries(parent, budget):
    by_name = defaultdict(list)
    with os.scandir(parent) as entries:
        for entry in entries:
            budget["entries"] += 1
            if budget["entries"] > MAX_ENTRIES:
                raise ScanLimit("The directory entry limit was exceeded.")
            if monotonic() > budget["deadline"]:
                raise ScanLimit("The discovery time budget was exceeded.")
            by_name[entry.name.casefold()].append((entry.name, entry.inode()))
    return by_name


def record_pair(parent, name, info, catalog_names, budget):
    if len(catalog_names) > 1:
        raise ValueError("The companion catalog has case-colliding filenames.")
    catalog = None
    if catalog_names:
        companion, inode = catalog_names[0]
        catalog_info = os.stat(companion, dir_fd=parent, follow_symlinks=False)
        if catalog_info.st_ino != inode:
            raise ValueError("The companion catalog changed after enumeration.")
        catalog = checked_file(parent, companion, catalog_info, MAX_CATALOG_BYTES, budget)
    rfa = checked_file(parent, name, info, MAX_FAMILY_BYTES, budget)
    if catalog is not None and file_stamp(catalog_info) != file_stamp(
            os.stat(companion, dir_fd=parent, follow_symlinks=False)):
        raise ValueError("The companion catalog changed during the paired read.")
    return rfa, catalog


def report_skip(skipped, relative, reason):
    skipped.append({"path": "/".join(relative) or ".", "reason": reason})


def walk(parent, relative, ancestors, budget, files, skipped):
    start = os.fstat(parent)
    checkpoint = len(files)
    by_name = classify_entries(parent, budget)
    for names in sorted(by_name.values(), key=lambda group: group[0][0].casefold()):
        name, inode = names[0]
        path = (*relative, name)
        if len(names) != 1:
            for member, _ in sorted(names):
                report_skip(skipped, (*relative, member), "Case-colliding filenames.")
            continue
        if ":" in name or "\\" in name:
            report_skip(skipped, path, "The path contains an unsupported component.")
            continue
        leaf_checkpoint = len(files)
        try:
            if monotonic() > budget["deadline"]:
                raise ScanLimit("The discovery time budget was exceeded.")
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if info.st_ino != inode:
                raise ValueError("The path changed after enumeration.")
            if link_or_reparse(info):
                report_skip(skipped, path, "Links and reparse paths are outside discovery.")
            elif stat.S_ISDIR(info.st_mode):
                if len(relative) >= MAX_DEPTH:
                    report_skip(skipped, path, "The directory exceeds the depth limit.")
                    continue
                with pinned_directory(parent, name, expected=info) as child:
                    walk(child, path, [*ancestors, identity(os.fstat(child))], budget, files, skipped)
            elif Path(name).suffix.casefold() == ".rfa":
                budget["families"] += 1
                if budget["families"] > MAX_FAMILIES:
                    raise ScanLimit("The family file count limit was exceeded.")
                candidates = by_name.get((Path(name).stem + ".txt").casefold(), [])
                rfa, catalog = record_pair(parent, name, info, candidates, budget)
                files.append({"path": "/".join(path), "ancestors": ancestors, "rfa": rfa, "catalog": catalog})
        except ScanLimit as error:
            report_skip(skipped, path, str(error))
            budget["limited"] = True
        except (OSError, ValueError) as error:
            del files[leaf_checkpoint:]
            report_skip(skipped, path, str(error))
        if budget["limited"]:
            break

    if directory_stamp(os.fstat(parent)) != directory_stamp(start):
        del files[checkpoint:]
        report_skip(skipped, relative, "Directory contents changed during discovery.")


def discover(raw_root):
    if not isinstance(raw_root, str) or not raw_root or ".." in Path(raw_root).parts:
        raise ValueError("Select one local folder without parent-directory traversal.")
    root = local_path(raw_root)
    budget = {"entries": 0, "families": 0, "bytes": 0, "deadline": monotonic() + MAX_SECONDS, "limited": False}
    files, skipped = [], []
    with pinned_root(root) as parent:
        root_identity = identity(os.fstat(parent))
        try:
            walk(parent, (), [root_identity], budget, files, skipped)
        except ScanLimit as error:
            report_skip(skipped, (), str(error))
    manifest = {
        "kind": "family-library-discovery", "version": 1, "root": str(root), "root_identity": root_identity,
        "complete": not skipped, "files": files, "skipped": skipped,
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    manifest["manifest_sha256"] = hashlib.sha256(encoded).hexdigest()
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", help="Approved ordinary local family-library folder")
    args = parser.parse_args()
    try:
        manifest = discover(args.root)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(manifest, ensure_ascii=True, indent=2))
    return 0 if manifest["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
