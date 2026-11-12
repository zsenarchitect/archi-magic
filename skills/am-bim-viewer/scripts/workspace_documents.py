"""Validate normalized project document reports and serve complete, bounded records."""

from dataclasses import dataclass, field
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from workspace_review_state import canonical


MAX_DOCUMENT_INPUT = 24 * 1024 * 1024 + 1
MAX_DOCUMENT_PUBLICATION = 25 * 1024 * 1024
MAX_DOCUMENT_PAGE = 16 * 1024 * 1024
MAX_DOCUMENT_RECORDS = 50
MAX_COLUMNS = 128
MAX_CELL = 4000
COLLECTIONS = ("mismatches", "missing_in_schedule", "actual_only")
COMPARISON_COUNTS = ("expected_rows", "schedule_rows", "mismatched_cells", "missing_in_schedule", "actual_only_rows")
MAX_GENERATION = 2**53 - 1


def text(value, limit=MAX_CELL):
    return isinstance(value, str) and len(value) <= limit


def positive(value):
    return type(value) is int and 0 < value <= MAX_GENERATION


def file_name(value):
    return text(value, 255) and value not in ("", ".", "..") and not any(c in value for c in "/\\\0")


def source_fields(source):
    if not isinstance(source, dict) or not file_name(source.get("file")) or not positive(source.get("header_row")):
        raise ValueError("Use document provenance from the local extraction CLI.")
    kind = source.get("format")
    if kind == "xlsx" and text(source.get("sheet"), 255) and source["sheet"]:
        return {key: source[key] for key in ("format", "file", "header_row", "sheet")}
    if kind in ("docx", "pdf") and positive(source.get("table")):
        keys = ("format", "file", "header_row", "table")
        if kind == "docx":
            return {key: source[key] for key in keys}
        if positive(source.get("page")):
            return {key: source[key] for key in (*keys, "page")}
    raise ValueError("Use a selected XLSX, DOCX or PDF table.")


def normalize_document(raw):
    if not isinstance(raw, dict):
        raise ValueError("Use a normalized project document report.")
    if raw.get("format") == "dwg":
        if set(raw) != {"format", "file", "revision"} or not file_name(raw["file"]) or not raw["file"].lower().endswith(".dwg") or not text(raw["revision"], 255) or not raw["revision"].strip():
            raise ValueError("Use a DWG reference with a display filename and revision.")
        return raw
    if set(raw) == {"source", "columns", "rows"}:
        return {"source": source_fields(raw["source"]), "columns": raw["columns"], "rows": raw["rows"]}
    if set(raw) == {"status", "document", "schedule", "counts", *COLLECTIONS}:
        schedule = raw["schedule"]
        if (not isinstance(schedule, dict) or not file_name(schedule.get("file"))
                or not text(schedule.get("schedule")) or not schedule["schedule"]):
            raise ValueError("Use schedule provenance from the local comparison CLI.")
        return {**raw, "document": source_fields(raw["document"]),
                "schedule": {key: schedule[key] for key in ("file", "schedule")}}
    raise ValueError("Use an extract, comparison, or DWG reference report.")


def summary(data):
    if data.get("format") == "dwg":
        return {"type": "reference", "source": data, "collections": {}}
    if "rows" in data:
        return {"type": "table", "source": data["source"], "collections": {"rows": len(data["rows"])}}
    return {"type": "comparison", "source": data["document"], "schedule": data["schedule"],
            "status": data["status"], "counts": data["counts"],
            "collections": {key: len(data[key]) for key in COLLECTIONS}}


def page_fields(data, collection, generation, digest, freshness):
    result = {"generation": generation, "digest": digest, "collection": collection, "freshness": freshness,
              "rows": [], "next_index": MAX_GENERATION, "total": len(data[collection])}
    if collection == "rows":
        result["columns"] = data["columns"]
    return result


def comparison_record(record, collection):
    keys = {"key", "source_row", "snapshot_row", "document_column", "schedule_column", "expected", "actual"}
    if collection != "mismatches":
        keys = {"key", "source_row" if collection == "missing_in_schedule" else "snapshot_row"}
    if not isinstance(record, dict) or set(record) != keys:
        return False
    identity = record["key"]
    return (isinstance(identity, dict) and 1 <= len(identity) <= MAX_COLUMNS
            and all(name and text(name) and text(value) for name, value in identity.items())
            and all(positive(record[field]) for field in keys & {"source_row", "snapshot_row"})
            and all(text(record[field]) for field in keys - {"key", "source_row", "snapshot_row"}))


def validate_document(data):
    if normalize_document(data) != data:
        raise ValueError("The document publication contains unexpected provenance fields.")
    if data.get("format") == "dwg":
        return
    if "rows" in data:
        columns = data["columns"]
        if (not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS
                or not all(text(column) and column for column in columns) or len(set(columns)) != len(columns)):
            raise ValueError("Use unique document column labels.")
        rows = data["rows"]
        if not isinstance(rows, list) or not rows:
            raise ValueError("Use a document table with records.")
        previous = data["source"]["header_row"]
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"source_row", "cells"} or not positive(row["source_row"])
                    or row["source_row"] <= previous or not isinstance(row["cells"], list)
                    or len(row["cells"]) != len(columns) or not all(text(value) for value in row["cells"])):
                raise ValueError("Use document records with intact source row numbers and cells.")
            previous = row["source_row"]
        collections = ("rows",)
    else:
        counts = data["counts"]
        if (not isinstance(counts, dict) or set(counts) != set(COMPARISON_COUNTS)
                or not all(type(value) is int and 0 <= value <= MAX_GENERATION for value in counts.values())
                or not all(isinstance(data[key], list) for key in COLLECTIONS)
                or [counts[key] for key in COMPARISON_COUNTS[2:]] != [len(data[key]) for key in COLLECTIONS]
                or data["status"] != ("differences" if any(data[key] for key in COLLECTIONS) else "matched")):
            raise ValueError("Use a complete document comparison with its original counts.")
        for collection in COLLECTIONS:
            if not all(comparison_record(row, collection) for row in data[collection]):
                raise ValueError("Use complete comparison records with row provenance.")
        collections = COLLECTIONS
    for collection in collections:
        overhead = len(canonical(page_fields(data, collection, MAX_GENERATION, "0" * 64, "stale")))
        if any(overhead + len(canonical(row)) > MAX_DOCUMENT_PAGE for row in data[collection]):
            raise ValueError("A document record exceeds the page byte limit.")


def encode_page(data, collection, start, generation, digest, freshness):
    if collection not in summary(data)["collections"]:
        raise LookupError("Document collection unavailable.")
    records = data[collection]
    if start < 0 or start > len(records):
        raise ValueError("The document page index is outside the collection.")
    page = page_fields(data, collection, generation, digest, freshness)
    size = len(canonical(page))
    end = start
    for row in records[start:start + MAX_DOCUMENT_RECORDS]:
        size += len(canonical(row)) + (end > start)
        if size > MAX_DOCUMENT_PAGE:
            break
        page["rows"].append(row)
        end += 1
    page["next_index"] = end if end < len(records) else None
    return canonical(page)


@dataclass(frozen=True)
class DocumentCopy:
    summary: dict
    directory: TemporaryDirectory = field(compare=False, repr=False)

    def read_page(self, collection, start, generation, digest, freshness):
        with (Path(self.directory.name) / "report.json").open("rb") as stream:
            data = json.load(stream)
        return encode_page(data, collection, start, generation, digest, freshness)


def copy_document(data):
    directory = TemporaryDirectory(prefix="archi-workspace-document-")
    try:
        (Path(directory.name) / "report.json").write_bytes(canonical(data))
        return DocumentCopy(summary(data), directory)
    except BaseException:
        directory.cleanup()
        raise
