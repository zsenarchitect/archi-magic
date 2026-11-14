"""Extract local project tables and compare them against a Revit schedule snapshot."""

import argparse
from collections import Counter
import json
import sys

from document_tables import MAX_CELLS, MAX_COLS, MAX_ROWS, cell_text, extract, local_bytes


def read_schedule(path, name):
    filename, raw = local_bytes(path)
    obj = json.loads(raw.decode("utf-8-sig"))
    if isinstance(obj, dict) and isinstance(obj.get("result"), dict):
        obj = obj["result"]
    if isinstance(obj, dict) and isinstance(obj.get("payload"), str):
        obj = json.loads(obj["payload"])
    if not isinstance(obj, dict) or not isinstance(obj.get("schedules"), list):
        raise ValueError("expected Revit schedules JSON or a schedule run record")
    matches = [row for row in obj["schedules"] if isinstance(row, dict) and row.get("name") == name]
    if len(matches) != 1:
        raise ValueError("select one schedule by its exact, unique name")
    schedule = matches[0]
    if schedule.get("itemized") is not True or schedule.get("truncated") is not False:
        raise ValueError("comparison requires an itemized, non-truncated schedule")
    columns, rows, kinds = (schedule.get(key) for key in ("columns", "rows", "rowKinds"))
    if (not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLS
            or any(not isinstance(c, str) for c in columns)):
        raise ValueError("schedule columns are missing or invalid")
    columns = [cell_text(c) for c in columns]
    if (not isinstance(rows, list) or not isinstance(kinds, list) or len(rows) != len(kinds)
            or not 1 <= len(rows) <= MAX_ROWS or len(columns) * len(rows) > MAX_CELLS
            or schedule.get("totalRows", len(rows)) != len(rows)):
        raise ValueError("schedule snapshot row metadata is missing or inconsistent")
    records = []
    excluded = Counter()
    for index, (row, kind) in enumerate(zip(rows, kinds), 1):
        if not isinstance(row, list) or len(row) != len(columns) or any(not isinstance(c, str) for c in row):
            raise ValueError(f"snapshot row {index} does not match the display table")
        if kind == "body":
            records.append({"snapshot_row": index, "cells": [cell_text(value) for value in row]})
        elif kind in ("blank", "group", "total"):
            excluded[kind] += 1
        else:
            raise ValueError(f"snapshot row {index} has an unsupported row kind")
    if not records:
        raise ValueError("the schedule snapshot contains no itemized body rows")
    return {
        "source": {"file": filename.name, "schedule": name, "schedule_id": schedule.get("id"),
                   "document": obj.get("document"), "revit_version": obj.get("revitVersion"),
                   "read_at": obj.get("readAt"), "excluded_snapshot_rows": dict(excluded),
                   "row_kind_source": obj.get("rowKindSource", "not specified")},
        "columns": columns, "rows": records,
    }


def mapping(value):
    left, separator, right = value.partition("=")
    if not separator or not left.strip() or not right.strip():
        raise argparse.ArgumentTypeError("use Expected Header=Schedule Header")
    return left.strip(), right.strip()


def index_columns(columns, names, side):
    indices = []
    for name in names:
        if columns.count(name) != 1:
            raise ValueError(f"{side} column {name!r} is absent or ambiguous")
        indices.append(columns.index(name))
    return indices


def index_records(records, indices, position):
    result = {}
    for row in records:
        key = tuple(row["cells"][index] for index in indices)
        if not all(key) or key in result:
            raise ValueError(f"{position} {row[position]} has a blank or duplicate record key")
        result[key] = row
    return result


def compare(document, schedule, keys, fields):
    mappings = keys + fields
    expected_names, actual_names = zip(*mappings)
    if len(set(expected_names)) != len(mappings) or len(set(actual_names)) != len(mappings):
        raise ValueError("use unique key and comparison columns on each side")
    expected_indexes = index_columns(document["columns"], expected_names, "document")
    actual_indexes = index_columns(schedule["columns"], actual_names, "schedule")
    expected = index_records(document["rows"], expected_indexes[:len(keys)], "source_row")
    actual = index_records(schedule["rows"], actual_indexes[:len(keys)], "snapshot_row")
    mismatches = []
    missing = []
    for key, source in expected.items():
        identity = dict(zip(expected_names[:len(keys)], key))
        target = actual.get(key)
        if target is None:
            missing.append({"key": identity, "source_row": source["source_row"]})
            continue
        for pair, left, right in zip(fields, expected_indexes[len(keys):], actual_indexes[len(keys):]):
            before, current = source["cells"][left], target["cells"][right]
            if before != current:
                mismatches.append({"key": identity, "source_row": source["source_row"],
                                   "snapshot_row": target["snapshot_row"],
                                   "document_column": pair[0], "schedule_column": pair[1],
                                   "expected": before, "actual": current})
    actual_only = [
        {"key": dict(zip(expected_names[:len(keys)], key)), "snapshot_row": row["snapshot_row"]}
        for key, row in actual.items() if key not in expected
    ]
    return {
        "status": "differences" if mismatches or missing or actual_only else "matched",
        "document": document["source"], "schedule": schedule["source"],
        "counts": {"expected_rows": len(expected), "schedule_rows": len(actual),
                   "mismatched_cells": len(mismatches), "missing_in_schedule": len(missing),
                   "actual_only_rows": len(actual_only)},
        "mismatches": mismatches, "missing_in_schedule": missing, "actual_only": actual_only,
    }


def positive_int(value):
    number = int(value)
    if not 1 <= number <= MAX_ROWS:
        raise argparse.ArgumentTypeError("expected a positive, bounded 1-based index")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    read = modes.add_parser("extract")
    check = modes.add_parser("compare")
    for mode in (read, check):
        mode.add_argument("document")
        mode.add_argument("--sheet")
        mode.add_argument("--page", type=positive_int)
        mode.add_argument("--table", type=positive_int)
        mode.add_argument("--header-row", type=positive_int, default=1)
    read.add_argument("--literal-text-column", help="Require literal text in the named XLSX column")
    check.add_argument("--schedule-json", required=True)
    check.add_argument("--schedule", required=True)
    check.add_argument("--key", type=mapping, action="append", required=True)
    check.add_argument("--field", type=mapping, action="append", required=True)
    args = parser.parse_args(argv)
    try:
        document = extract(args.document, args.sheet, args.page, args.table, args.header_row,
                           literal_text_column=getattr(args, "literal_text_column", None))
        result = document if args.mode == "extract" else compare(
            document, read_schedule(args.schedule_json, args.schedule), args.key, args.field)
        encoded = json.dumps(result, ensure_ascii=True, indent=2)
        if len(encoded) > 24 * 1024 * 1024:
            raise ValueError("report exceeds output limits")
    except ImportError as exc:
        print(f"ERROR: install requirements-documents.txt: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {str(exc)[:400]}", file=sys.stderr)
        return 2
    print(encoded)
    return 1 if result.get("status") == "differences" else 0


if __name__ == "__main__":
    raise SystemExit(main())
