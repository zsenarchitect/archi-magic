"""Bind a batch family type CSV to an exact, reviewed library discovery manifest."""

import argparse
import csv
import hashlib
from io import BytesIO, StringIO
import json
import re
import sys
from time import monotonic

from family_library_discovery import MAX_SECONDS, checked_file, child_info, manifest_digest, pinned_root, root_path
from read_family_library import read_request_file
from stage_family_library import parts, selected_rows

MAX_TABLE_BYTES = 1024 * 1024
MAX_FAMILIES = 50
MAX_ROWS = 250
MAX_COLUMNS = 50
IDENTITY_COLUMNS = ["source_path", "seed_type", "type"]
SIZE_HEADER = re.compile(r"(.+)\[(mm|cm|m|in|ft|1)\]")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")


def exact_text(value, location):
    if not value.strip() or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{location} needs a bounded exact name without control characters.")
    return value


def size_columns(header):
    if header[:3] != IDENTITY_COLUMNS or not 1 <= len(header) - 3 <= MAX_COLUMNS:
        raise ValueError("Use source_path,seed_type,type followed by one to 50 parameter[unit] columns.")
    columns = []
    seen = set()
    for raw in header[3:]:
        match = SIZE_HEADER.fullmatch(raw)
        if match is None:
            raise ValueError("Use an explicit unit in each size header: mm, cm, m, in, ft, or 1.")
        name = exact_text(match[1], "Size parameter")
        key = name.casefold()
        if key in seen:
            raise ValueError("The size parameter headers have an ambiguous duplicate name.")
        seen.add(key)
        columns.append({"parameter": name, "unit": match[2]})
    return columns


def bind_table(raw, discovery):
    if not 0 < len(raw) <= MAX_TABLE_BYTES:
        raise ValueError("Use a nonempty UTF-8 table up to 1 MiB.")
    try:
        stream = StringIO(raw.decode("utf-8-sig"), newline="")
    except UnicodeError as error:
        raise ValueError("The family type table must use UTF-8.") from error
    reader = csv.reader(stream, strict=True)
    rows, keys, sources = [], set(), {}
    try:
        columns = size_columns(next(reader, []))
        for values in reader:
            line = reader.line_num
            if len(rows) >= MAX_ROWS:
                raise ValueError("The family type table exceeds 250 rows.")
            if len(values) != len(columns) + len(IDENTITY_COLUMNS):
                raise ValueError(f"CSV line {line} does not have the exact number of required fields.")
            source, seed, target = [exact_text(value, f"CSV line {line} {name}")
                                    for name, value in zip(IDENTITY_COLUMNS, values)]
            parts(source)
            key = (source.casefold(), target.casefold())
            if key in keys:
                raise ValueError(f"CSV line {line} repeats a requested type for the same family source.")
            keys.add(key)
            sources[source] = None
            if len(sources) > MAX_FAMILIES:
                raise ValueError("The family type table exceeds 50 sources.")
            sizes = values[len(IDENTITY_COLUMNS):]
            if any(len(value) > 64 or not NUMBER.fullmatch(value) for value in sizes):
                raise ValueError(f"CSV line {line} requires finite decimal size values.")
            rows.append({"line": line, "source_path": source, "seed_type": seed, "type": target,
                         "sizes": [dict(column, value=value) for column, value in zip(columns, sizes)]})
    except csv.Error as error:
        raise ValueError(f"Invalid CSV near line {reader.line_num}.") from error
    if not rows:
        raise ValueError("Use at least one family type row.")
    reviewed = selected_rows(discovery, list(sources))
    request = {
        "kind": "batch-family-type-request", "version": 1, "table_sha256": hashlib.sha256(raw).hexdigest(),
        "discovery_sha256": discovery["manifest_sha256"], "root": discovery["root"],
        "root_identity": discovery["root_identity"], "sources": reviewed, "rows": rows,
    }
    return dict(request, request_sha256=manifest_digest(request))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True)
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    try:
        manifest = root_path(args.manifest)
        with pinned_root(manifest.parent) as parent:
            discovery = read_request_file(parent, manifest.name)
        table = root_path(args.table)
        data = BytesIO()
        with pinned_root(table.parent) as parent:
            checked_file(parent, table.name, child_info(parent, table.name), MAX_TABLE_BYTES,
                         {"bytes": 0, "deadline": monotonic() + MAX_SECONDS}, data)
        result = bind_table(data.getvalue(), discovery)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    except (AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
