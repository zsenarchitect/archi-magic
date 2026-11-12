"""Inspect a bounded saved Xref graph under an operator-approved drawing root."""

from collections import defaultdict, deque
from contextlib import ExitStack, contextmanager
import hashlib
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from time import monotonic

from cad_file_inventory import FILE_LIMIT, RESERVED, decode_snapshot, finish_inventory, local_path


MAX_DRAWINGS = 32
MAX_DEPTH = 8
MAX_EDGES = 512
MAX_BYTES = 128 * 1024 * 1024
MAX_SECONDS = 60
MAX_PARTS = 64
MAX_PATH = 1024
SUPPORTED_SUFFIXES = {".dxf", ".dwg"}


def descriptor_support():
    return os.name == "posix" and hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd


@contextmanager
def physical_root(path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with ExitStack() as handles:
        descriptor = os.open(path.anchor, flags)
        handles.callback(os.close, descriptor)
        for part in path.parts[1:]:
            descriptor = os.open(part, flags, dir_fd=descriptor)
            handles.callback(os.close, descriptor)
        yield descriptor


def reference_parts(path):
    if not path or len(path) > MAX_PATH:
        raise ValueError("Invalid saved path.")
    parts = path.replace("\\", "/").split("/")
    if len(parts) > MAX_PARTS or any(
        not part or not part.isprintable() or RESERVED.match(part)
        or any(c in part for c in ':*?<>|"') or (part not in (".", "..") and part.endswith((" ", ".")))
        for part in parts
    ):
        raise ValueError("Invalid saved path.")
    return parts


@contextmanager
def open_drawing(root, parts):
    if (not parts or parts[-1] in (".", "..") or len(parts) > MAX_PARTS
            or len("/".join(parts)) > MAX_PATH or Path(parts[-1]).suffix.lower() not in SUPPORTED_SUFFIXES):
        raise ValueError("Unsupported drawing path.")
    device = os.fstat(root).st_dev
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with ExitStack() as handles:
        stack, resolved = [root], []
        for part in parts[:-1]:
            if part == ".":
                continue
            if part == "..":
                if len(stack) == 1:
                    raise ValueError("The reference is outside the approved root.")
                stack.pop()
                resolved.pop()
                continue
            directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=stack[-1])
            handles.callback(os.close, directory)
            if os.fstat(directory).st_dev != device:
                raise ValueError("A reference crosses the approved filesystem boundary.")
            stack.append(directory)
            resolved.append(part)
        descriptor = os.open(parts[-1], flags, dir_fd=stack[-1])
        handles.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_dev != device or info.st_nlink != 1:
            raise ValueError("A reference is outside the regular single-link file scope.")
        identity = (info.st_dev, info.st_ino, os.fstat(stack[-1]).st_ino)
        yield descriptor, info, (*resolved, parts[-1]), identity


def file_state(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def stage_drawing(root, root_path, parts, descriptor, info, snapshot):
    digest = hashlib.sha256()
    copied = 0
    with os.fdopen(os.dup(descriptor), "rb") as stream, snapshot.open("xb") as target:
        while chunk := stream.read(min(1024 * 1024, info.st_size - copied + 1)):
            copied += len(chunk)
            if copied > info.st_size:
                raise ValueError("The drawing grew during staging.")
            digest.update(chunk)
            target.write(chunk)
    with physical_root(root_path) as current_root, open_drawing(root, parts) as (_, current, _, _):
        if (not os.path.samestat(os.fstat(root), os.fstat(current_root)) or copied != info.st_size
                or file_state(info) != file_state(os.fstat(descriptor)) or file_state(info) != file_state(current)):
            raise ValueError("The approved drawing path changed during staging.")
    return digest.hexdigest()


def mark_cycles(root, adjacency):
    active, visited = {}, set()
    chain = []

    def visit(node):
        active[node] = len(chain)
        visited.add(node)
        for target, edge in adjacency[node]:
            if target in active:
                cycle = chain[active[target]:] + [edge]
                edge["isCircular"] = True
                edge["cycle"] = [part["host"] for part in cycle] + [edge["target"]]
            elif target not in visited:
                chain.append(edge)
                visit(target)
                chain.pop()
        active.pop(node)

    visit(root)


class XrefScan:
    def __init__(self, root, path, decoder, temporary):
        self.root = root
        self.path = path
        self.decoder = decoder
        self.temporary = temporary
        self.deadline = monotonic() + MAX_SECONDS
        self.attempted = {}
        self.bytes = 0
        self.edges = []
        self.incomplete = False

    def load(self, parts, descriptor, info, identity):
        if len(self.attempted) >= MAX_DRAWINGS or self.bytes + info.st_size > MAX_BYTES:
            raise ValueError("The drawing count or aggregate source-byte limit was reached.")
        self.attempted[identity] = None
        self.bytes += info.st_size
        if info.st_size > FILE_LIMIT or monotonic() >= self.deadline:
            raise ValueError("The drawing or scan exceeds its limits.")
        snapshot = self.temporary / ("drawing" + Path(parts[-1]).suffix.lower())
        try:
            digest = stage_drawing(self.root, self.path, parts, descriptor, info, snapshot)
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise ValueError("The decoder budget was exhausted.")
            result, tool = decode_snapshot(snapshot, self.decoder, timeout=remaining)
        except FileNotFoundError as error:
            raise ValueError("An approved drawing or decoder disappeared during the scan.") from error
        finally:
            snapshot.unlink(missing_ok=True)
        self.attempted[identity] = file_state(info)
        return result, tool, digest

    def edge(self, host, xref, depth, pending, adjacency):
        host_parts, host_id = host
        mode = xref["mode"]
        row = {"host": "/".join(host_parts), "name": xref["name"][:MAX_PATH], "path": xref["path"][:MAX_PATH],
               "mode": mode, "status": "unchecked", "isCircular": False}
        self.edges.append(row)
        if depth and mode == "overlay":
            row["status"] = "overlay_excluded"
            return
        if not xref["flags_consistent"] or mode not in ("attach", "overlay"):
            row["reason"] = "uncertain_reference_metadata"
            self.incomplete = True
            return
        try:
            parts = [*host_parts[:-1], *reference_parts(xref["path"])]
            with open_drawing(self.root, parts) as (descriptor, info, resolved, identity):
                row["target"] = "/".join(resolved)
                if identity not in self.attempted:
                    if depth >= MAX_DEPTH:
                        raise ValueError("The scan depth limit was reached.")
                    result, _, digest = self.load(parts, descriptor, info, identity)
                    self.incomplete |= "The drawing decoder reported warnings." in result["notes"]
                    pending.append((resolved, identity, result["xrefs"][:MAX_EDGES + 1], depth + 1))
                    row["sha256"] = digest
                elif self.attempted[identity] != file_state(info):
                    raise ValueError("The reference could not be verified against the cached drawing.")
                row["status"] = "present"
                adjacency[host_id].append((identity, row))
        except FileNotFoundError:
            row["status"] = "missing"
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
            row["reason"] = "decode_or_scan_limit" if "target" in row else "unsupported_or_unsafe_saved_path"
            self.incomplete = True

    def inspect(self, source, parts):
        with open_drawing(self.root, parts) as (descriptor, info, resolved, identity):
            result, tool, digest = self.load(parts, descriptor, info, identity)
        self.incomplete |= "The drawing decoder reported warnings." in result["notes"]
        references = result["xrefs"]
        pending = deque([(resolved, identity, references[:MAX_EDGES + 1], 0)])
        adjacency = defaultdict(list)
        while pending:
            host, host_id, xrefs, depth = pending.popleft()
            for xref in xrefs:
                if len(self.edges) >= MAX_EDGES:
                    self.incomplete = True
                    pending.clear()
                    break
                self.edge((host, host_id), xref, depth, pending, adjacency)
        mark_cycles(identity, adjacency)
        root_name = "/".join(resolved)
        result["direct_xref_count"] = len(references)
        result["xrefs"] = [edge for edge in self.edges if edge["host"] == root_name]
        result["xref_graph"] = self.edges
        if self.incomplete:
            result["notes"].append("Some nested Xrefs were outside the verified saved-path scope.")
        return finish_inventory(result, source, digest, tool,
                                xref_scope="nested_incomplete" if self.incomplete else "nested_saved_paths")


def inspect_nested_drawing(raw_source, raw_root, decoder):
    if not descriptor_support():
        raise ValueError("Nested Xref scanning requires POSIX descriptor support. Rerun without --xref-root.")
    root, source = local_path(raw_root), local_path(raw_source)
    parts = source.relative_to(root).parts
    if ".." in root.parts or any(part in (".", "..") for part in parts):
        raise ValueError("Use physical root and source paths.")
    with physical_root(root) as directory, TemporaryDirectory(prefix="archi-cad-xrefs-") as temporary:
        return XrefScan(directory, root, decoder, Path(temporary)).inspect(source, parts)
