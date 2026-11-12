"""Bounded read-only inventory of local DXF and DWG drawing files."""

from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic

FILE_LIMIT = 32 * 1024 * 1024
LOG_LIMIT = 128 * 1024
RECORD_LIMIT = 150000
VERSIONS = {"AC1015", "AC1032"}
LAYOUT_PREFIXES = ("*model_space", "*paper_space")
RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def local_path(raw):
    windows = PureWindowsPath(raw)
    if (
        raw.startswith(("//", "\\\\"))
        or (":" in raw and not (os.name == "nt" and re.match(r"^[a-z]:[/\\]", raw, re.I)))
        or ":" in raw[2:]
        or (windows.drive and not windows.root)
        or (raw.startswith("\\") and not windows.drive)
    ):
        raise ValueError("Select a local filesystem path.")
    return Path(raw).absolute()


def link_or_reparse(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def snapshot_file(source, snapshot, *, limit=FILE_LIMIT):
    info = source.lstat()
    if link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Select an original regular drawing file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    digest = hashlib.sha256()
    copied = 0
    with os.fdopen(os.open(source, flags), "rb") as origin, snapshot.open("xb") as target:
        before = os.fstat(origin.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError(f"The source must be a regular file up to {limit} bytes.")
        while chunk := origin.read(1024 * 1024):
            copied += len(chunk)
            if copied > limit:
                raise ValueError(f"The source exceeds {limit} bytes.")
            digest.update(chunk)
            target.write(chunk)
        after = os.fstat(origin.fileno())
        current = source.lstat()
        if not (
            copied == before.st_size == after.st_size == current.st_size
            and before.st_mtime_ns == after.st_mtime_ns == current.st_mtime_ns
            and os.path.samestat(before, current)
        ):
            raise ValueError("Drawing changed during the read.")
    return digest.hexdigest()


def run_decoder(args, directory, *, timeout=35):
    buffers = [bytearray(), bytearray()]
    exceeded = Event()

    def drain(pipe, buffer, limit):
        with pipe:
            while chunk := pipe.read1(16384):
                if len(buffer) + len(chunk) > limit:
                    exceeded.set()
                    return
                buffer.extend(chunk)

    with subprocess.Popen(
        args, cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        threads = [
            Thread(target=drain, args=(pipe, buffer, limit), daemon=True)
            for pipe, buffer, limit in zip(
                (process.stdout, process.stderr), buffers, (FILE_LIMIT, LOG_LIMIT)
            )
        ]
        for thread in threads:
            thread.start()
        deadline = monotonic() + timeout
        try:
            while process.poll() is None and not exceeded.wait(0.02):
                if monotonic() >= deadline:
                    raise ValueError("Drawing decoder timed out.")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            for thread in threads:
                thread.join()
        if exceeded.is_set():
            raise ValueError("Drawing decoder exceeded its output limit.")
        if process.returncode or re.search(rb"^\s*(ERROR|FATAL)\b", buffers[1], re.M | re.I):
            raise ValueError("Drawing decoder failed or reported invalid drawing data.")
    return bytes(buffers[0]), bytes(buffers[1])


def validate_json(raw, *, dwg=False):
    def pairs_hook(pairs):
        values = {}
        for name, value in pairs:
            # LibreDWG repeats metadata that the audit does not consume.
            if name in values and not (dwg and name in {"", "_subclass", "type"}):
                raise ValueError("Decoder produced repeated JSON fields.")
            values[name] = value
        return values

    def invalid_constant(_):
        raise ValueError("Drawing decoder output contains a nonfinite number.")

    def validate(value, depth):
        if depth > 24:
            raise ValueError("Decoder output exceeds JSON structure limits.")
        if isinstance(value, (dict, list)):
            if len(value) > RECORD_LIMIT:
                raise ValueError("Decoder output exceeds JSON structure limits.")
            for item in value.values() if isinstance(value, dict) else value:
                validate(item, depth + 1)

    obj = json.loads(raw, object_pairs_hook=pairs_hook, parse_constant=invalid_constant)
    validate(obj, 0)
    return obj


def read_xref_path(name, source):
    parts = name.replace("\\", "/").split("/")
    if parts and parts[0] == ".":
        parts.pop(0)
    if (
        not parts or any(
            not part or part in (".", "..") or part.endswith((" ", "."))
            or any(char in part for char in ':*?<>|"') or RESERVED.match(part)
            or not part.isprintable()
            for part in parts
        )
    ):
        return "unchecked", False
    current = source.parent
    for index, part in enumerate(parts):
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return "missing", False
        except OSError:
            return "unchecked", False
        if link_or_reparse(info):
            return "unchecked", False
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            return "unchecked", False
    if not stat.S_ISREG(info.st_mode):
        return "unchecked", False
    return "present", os.path.samestat(info, source.lstat())


def absolute_ref(raw):
    return raw[3] if isinstance(raw, list) and len(raw) == 4 and type(raw[3]) is int else None


def handle(raw):
    if isinstance(raw, list) and len(raw) == 3 and type(raw[2]) is int:
        return raw[2]
    raise ValueError("Invalid native drawing handle.")


def text_value(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("Invalid or overlong drawing metadata.")
    return value


def dwg_inventory(doc, version):
    if doc["FILEHEADER"]["version"] != version or doc["created_by"] != "LibreDWG 0.14":
        raise ValueError("Unexpected DWG decoder version or source signature.")
    records = doc["OBJECTS"]
    if not isinstance(records, list) or not 1 <= len(records) <= RECORD_LIMIT:
        raise ValueError("Drawing exceeds the supported object limits.")
    selected = {}
    for record in records:
        if record.get("object") in {"LAYER", "LTYPE", "BLOCK_HEADER"} or record.get("entity") in {
            "ATTRIB", "ATTDEF", "INSERT", "MINSERT",
        }:
            key = handle(record["handle"])
            if key in selected:
                raise ValueError("Relevant drawing records have duplicate handles.")
            selected[key] = record

    # DWG stores structural delimiters as entities with layers. DXF iteration
    # omits them, and they do not represent drawn or placed content.
    layer_counts = Counter(
        absolute_ref(record["layer"]) for record in records
        if "layer" in record and record.get("entity") not in {"BLOCK", "ENDBLK", "SEQEND"}
    )
    layers, xrefs, blocks, notes = [], [], {}, []
    values, definitions = [], []
    for key, record in selected.items():
        kind = record.get("object")
        if kind == "LAYER":
            linetype = selected.get(absolute_ref(record.get("ltype")), {})
            layers.append({"name": text_value(record["name"]), "entityCount": layer_counts[key],
                           "color": record.get("color"), "linetype": linetype.get("name")})
        if kind == "BLOCK_HEADER":
            name = text_value(record["name"])
            flagged = bool(record.get("blkisxref") or record.get("xrefoverlaid"))
            path = text_value(record.get("xref_pname", ""))
            if flagged or path:
                xrefs.append({"name": name, "path": path, "flags_consistent": bool(path) == flagged})
            elif not name.casefold().startswith(LAYOUT_PREFIXES):
                blocks[key] = {"name": name, "count": 0, "attributes": [], "definitions": []}
    for key, record in selected.items():
        kind = record.get("entity")
        if kind in {"INSERT", "MINSERT"}:
            ref = absolute_ref(record.get("block_header"))
            if ref in blocks:
                blocks[ref]["count"] += 1
            elif selected.get(ref, {}).get("object") != "BLOCK_HEADER":
                notes.append("Some inserts could not be assigned to a block definition.")
        if kind in {"ATTRIB", "ATTDEF"}:
            is_value = kind == "ATTRIB"
            owner_id = absolute_ref(record.get("ownerhandle"))
            owner = selected.get(owner_id, {})
            block_id = absolute_ref(owner.get("block_header")) if is_value else owner_id
            row = {"handle": key, "tag": text_value(record["tag"]), "value": text_value(
                record.get("text_value" if is_value else "default_value", ""))}
            if block_id in blocks and (not is_value or owner.get("entity") in {"INSERT", "MINSERT"}):
                row["insert_handle"] = owner_id if is_value else None
                blocks[block_id]["attributes" if is_value else "definitions"].append(row)
            else:
                (values if is_value else definitions).append(row)
    if values or definitions:
        notes.append("Some DWG attributes or definitions could not be attributed to a block.")
    header = {"$INSUNITS": doc["HEADER"].get("INSUNITS"),
              "$MEASUREMENT": doc.get("Template", {}).get("MEASUREMENT")}
    return {"layers": layers, "xrefs": xrefs, "blocks": list(blocks.values()), "header": header,
            "unassociated_attributes": values, "unassociated_definitions": definitions, "notes": notes}


def dxf_worker(snapshot):
    import ezdxf
    from ezdxf.blkrefs import find_unreferenced_blocks
    if ezdxf.__version__ != "1.4.5":
        raise ValueError("Install the qualified ezdxf 1.4.5 reader.")
    doc = ezdxf.readfile(snapshot)
    counts = Counter()
    inserts = defaultdict(list)
    if len(doc.entitydb) > RECORD_LIMIT:
        raise ValueError("Drawing exceeds the supported object limits.")
    layers, xrefs, blocks, purge_candidates = [], [], [], []
    unreferenced = find_unreferenced_blocks(doc)
    for layout in doc.blocks:
        for entity in layout:
            if entity.dxf.hasattr("layer"):
                counts[entity.dxf.layer.casefold()] += 1
            if entity.dxftype() == "INSERT":
                inserts[entity.dxf.name.casefold()].append(entity)
    for layer in doc.layers:
        name = text_value(layer.dxf.name)
        layers.append({"name": name, "entityCount": counts[name.casefold()],
                       "color": layer.dxf.color, "linetype": layer.dxf.linetype,
                       "true_color": layer.dxf.get("true_color", None)})
    for layout in doc.blocks:
        entity = layout.block
        name = text_value(layout.name)
        path = text_value(entity.dxf.xref_path)
        flagged = entity.is_xref or entity.is_xref_overlay
        if flagged or path:
            xrefs.append({"name": name, "path": path, "flags_consistent": bool(path) == flagged})
        elif not entity.is_layout_block:
            if (layout.name in unreferenced and not name.startswith(("*", "_")) and "|" not in name
                    and not entity.is_anonymous and not entity.dxf.flags & (entity.EXTERNAL | entity.RESOLVED)):
                purge_candidates.append({"kind": "block", "name": name})
            instances = inserts.get(name.casefold(), [])
            attributes = [
                {"tag": text_value(attribute.dxf.tag), "value": text_value(attribute.dxf.text),
                 "insert_handle": insert.dxf.handle}
                for insert in instances for attribute in insert.attribs
            ]
            definitions = [
                {"tag": text_value(attribute.dxf.tag), "value": text_value(attribute.dxf.text)}
                for attribute in layout.query("ATTDEF")
            ]
            blocks.append({"name": name, "count": len(instances), "attributes": attributes,
                           "definitions": definitions})
    return {"layers": layers, "xrefs": xrefs, "blocks": blocks,
            "purge_candidates": sorted(purge_candidates, key=lambda row: row["name"].casefold()),
            "header": {key: doc.header.get(key) for key in ("$INSUNITS", "$MEASUREMENT")},
            "unassociated_attributes": [], "unassociated_definitions": [], "notes": []}


def inspect_drawing(raw_source, decoder):
    source = local_path(raw_source)
    source = source.parent.resolve(strict=True) / source.name
    suffix = source.suffix.lower()
    with TemporaryDirectory(prefix="archi-cad-") as temporary:
        work = Path(temporary)
        snapshot = work / ("drawing" + suffix)
        digest = snapshot_file(source, snapshot)
        if suffix == ".dwg":
            if not decoder or not Path(decoder).is_absolute():
                raise ValueError("Give the absolute path to the trusted LibreDWG 0.14 dwgread executable.")
            exe = local_path(decoder)
            if not exe.is_file():
                raise ValueError("The selected DWG decoder does not exist.")
            with snapshot.open("rb") as stream:
                version = stream.read(6).decode("ascii")
            if version not in VERSIONS:
                raise ValueError("The qualified DWG versions are AC1015 and AC1032.")
            raw, log = run_decoder([str(exe), "-O", "JSON", str(snapshot)], work)
            result = dwg_inventory(validate_json(raw, dwg=True), version)
            tool = "LibreDWG 0.14"
        else:
            raw, log = run_decoder([
                sys.executable, str(Path(__file__).resolve()), "_dxf_worker", str(snapshot),
            ], work)
            result = validate_json(raw)
            tool = "ezdxf 1.4.5"
        if re.search(rb"^\s*WARN(?:ING)?\b", log, re.M | re.I):
            result["notes"].append("The drawing decoder reported warnings.")

    for xref in result["xrefs"]:
        status, circular = read_xref_path(xref["path"], source) if xref["flags_consistent"] else (
            "unchecked", False,
        )
        xref.update(status=status, isCircular=circular)
    if result["xrefs"]:
        result["notes"].append("Nested Xrefs and cycles were not inspected.")
    if any(xref["status"] == "unchecked" for xref in result["xrefs"]):
        result["notes"].append("Some Xref paths were outside the supported local path scope.")
    if (type(result["header"]["$INSUNITS"]) is not int or not 0 <= result["header"]["$INSUNITS"] <= 7
            or type(result["header"]["$MEASUREMENT"]) is not int or result["header"]["$MEASUREMENT"] not in (0, 1)):
        result["notes"].append("One or more drawing unit settings were absent or outside the supported range.")
    result["notes"] = list(dict.fromkeys(result["notes"]))
    result.update(filename=source.name, sha256=digest, decoder=tool, coverage={
        "xref_scope": "drawing_directory_only",
        "layer_style_policy": "not_assessed",
        "required_attribute_policy": "not_assessed",
        "purge_eligibility": "not_assessed",
        "block_purge_review": "known_dxf_references" if suffix == ".dxf" else "not_assessed",
    })
    return result


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "_dxf_worker":
    try:
        print(json.dumps(dxf_worker(sys.argv[2]), ensure_ascii=True))
    except ImportError:
        print("Optional ezdxf 1.4.5 not available", file=sys.stderr)
        sys.exit(2)
