"""Extract explicitly selected tables from local project documents."""

from datetime import date, datetime, time
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from zipfile import ZipFile


MAX_FILE = 24 * 1024 * 1024
MAX_MEMBER = 12 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLS = 128
MAX_CELLS = 150000
MAX_CELL_TEXT = 4000
MAX_TABLE_TEXT = 8 * 1024 * 1024
MAX_PDF_PAGE = 200
PDF_TIMEOUT = 20
CELL_ADDRESS = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")


def local_bytes(raw_path):
    if re.match(r"^[a-z][a-z0-9+.-]*:", raw_path, re.I) and not re.match(r"^[a-z]:[\\/]", raw_path, re.I):
        raise ValueError("choose a local file path, not a URI")
    if raw_path.startswith(("\\\\", "//")):
        raise ValueError("choose a local file or a local synced copy, not a UNC path")
    path = Path(raw_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("choose a regular local file, not a symlink")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise ValueError("input exceeds the local file limits")
        data = stream.read(MAX_FILE + 1)
    if len(data) != info.st_size or len(data) > MAX_FILE:
        raise ValueError("input file changed while being read")
    return path, data


def cell_coordinates(address):
    match = CELL_ADDRESS.fullmatch(address)
    if not match:
        raise ValueError("invalid spreadsheet cell address")
    column = 0
    for char in match[1]:
        column = column * 26 + ord(char) - ord("A") + 1
    row = int(match[2])
    if column > MAX_COLS or row > MAX_ROWS:
        raise ValueError("spreadsheet cell exceeds table bounds")
    return row, column


def validate_ooxml(data):
    from defusedxml import ElementTree

    with ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 1000 or sum(info.file_size for info in infos) > MAX_ARCHIVE:
            raise ValueError("Office archive exceeds expansion limits")
        seen = set()
        expanded = 0
        cells = merged_cells = 0
        for info in infos:
            name = info.filename
            alias = name.rstrip("/").casefold()
            parts = name.rstrip("/").split("/")
            if (name != info.orig_filename or alias in seen or len(name) > 250 or "\\" in name
                    or ":" in name or any(part in {"", ".", ".."} for part in parts)
                    or any(ord(c) < 32 for c in name) or info.flag_bits & 1
                    or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError("unsafe or duplicate Office archive member")
            seen.add(alias)
            if info.is_dir():
                continue
            limit = min(MAX_MEMBER, MAX_ARCHIVE - expanded)
            if info.file_size > limit:
                raise ValueError("Office archive member exceeds expansion limits")
            with archive.open(info) as stream:
                payload = stream.read(limit + 1)
            if len(payload) != info.file_size or len(payload) > limit:
                raise ValueError("invalid Office archive member size")
            expanded += len(payload)
            if not name.lower().endswith((".xml", ".rels")):
                continue
            root = ElementTree.fromstring(payload, forbid_dtd=True, forbid_entities=True,
                                          forbid_external=True)
            if name.lower().endswith(".rels"):
                for node in root:
                    if (node.get("TargetMode", "").lower() == "external"
                            and not node.get("Type", "").endswith("/hyperlink")):
                        raise ValueError("external Office relationships other than hyperlinks are unsupported")
            if root.tag == "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}worksheet":
                prefix = root.tag.split("}")[0] + "}"
                addresses = set()
                for node in root.iter(prefix + "row"):
                    row = node.get("r")
                    if row is not None and (not row.isascii() or not row.isdigit()
                                            or len(row) > 4 or not 1 <= int(row) <= MAX_ROWS):
                        raise ValueError("spreadsheet row exceeds limits")
                    for cell in node.findall(prefix + "c"):
                        cells += 1
                        position = cell_coordinates(cell.get("r", ""))
                        if cells > MAX_CELLS or position in addresses:
                            raise ValueError("duplicate or excessive spreadsheet cells")
                        if row is not None and position[0] != int(row):
                            raise ValueError("spreadsheet cell address does not match its row")
                        addresses.add(position)
                for node in root.iter(prefix + "mergeCell"):
                    bounds = node.get("ref", "").split(":")
                    if len(bounds) != 2:
                        raise ValueError("invalid spreadsheet merged range")
                    start, end = map(cell_coordinates, bounds)
                    if start[0] > end[0] or start[1] > end[1]:
                        raise ValueError("invalid spreadsheet merged range")
                    merged_cells += (end[0] - start[0] + 1) * (end[1] - start[1] + 1)
                    if merged_cells > MAX_CELLS:
                        raise ValueError("spreadsheet merged ranges exceed limits")


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if not isinstance(value, (str, bool, int, float)):
        raise ValueError("unsupported cell value")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite cell value")
    value = str(value)
    if len(value) > MAX_CELL_TEXT:
        raise ValueError("cell text exceeds size limit")
    return value.strip()


def xlsx_rows(data, sheet_name, *, header=1, literal_text_column=None):
    import openpyxl

    with io.BytesIO(data) as stream:
        book = openpyxl.load_workbook(stream, keep_links=False, data_only=False)
        try:
            if sheet_name not in book.sheetnames:
                raise ValueError("named sheet was not found")
            sheet = book[sheet_name]
            if not hasattr(sheet, "merged_cells") or sheet.merged_cells.ranges:
                raise ValueError("select an unmerged worksheet table")
            if sheet.max_row > MAX_ROWS or sheet.max_column > MAX_COLS:
                raise ValueError("worksheet exceeds table bounds")
            literal_index = None
            for index, row in enumerate(sheet.iter_rows(), 1):
                if any(cell.data_type in {"e", "f"} for cell in row):
                    raise ValueError(f"spreadsheet row {index} has a formula or spreadsheet error")
                values = [cell_text(cell.value) for cell in row]
                if literal_text_column is not None:
                    if index == header:
                        if not literal_text_column or values.count(literal_text_column) != 1:
                            raise ValueError("select one existing, unique literal text column")
                        literal_index = values.index(literal_text_column)
                    elif literal_index is not None and any(values):
                        cell = row[literal_index]
                        if (cell.data_type not in {"s", "inlineStr"} or not isinstance(cell.value, str)
                                or not cell.value or cell.value.strip() != cell.value):
                            raise ValueError(f"spreadsheet row {index} requires a nonempty literal text cell "
                                             "without surrounding whitespace in the selected column")
                yield index, values
        finally:
            book.close()


def docx_rows(data, table_number):
    from docx import Document

    with io.BytesIO(data) as stream:
        document = Document(stream)
        if table_number > len(document.tables):
            raise ValueError("Word table was not found")
        table = document.tables[table_number - 1]
        if len(table.rows) > MAX_ROWS or len(table.columns) > MAX_COLS:
            raise ValueError("Word table exceeds bounds")
        for index, row in enumerate(table.rows, 1):
            if (row.grid_cols_before or row.grid_cols_after
                    or any(tc.vMerge is not None or tc.grid_span != 1 for tc in row._tr.tc_lst)):
                raise ValueError(f"Word table row {index} has merged or omitted cells")
            cells = row.cells
            if any(cell.tables for cell in cells):
                raise ValueError(f"Word table row {index} has a nested table")
            yield index, [cell_text(cell.text) for cell in cells]


def pdf_worker(page_number, table_number, data):
    import pdfplumber

    if not 1 <= page_number <= MAX_PDF_PAGE:
        raise ValueError("PDF page exceeds the supported page range")
    with io.BytesIO(data) as stream, pdfplumber.open(stream, pages=[page_number], repair=False) as pdf:
        if not pdf.pages:
            raise ValueError("PDF page was not found")
        page = pdf.pages[0]
        if not page.chars:
            raise ValueError("PDF page has no selectable text")
        tables = page.extract_tables()
        if table_number > len(tables):
            raise ValueError("PDF table was not found")
        selected = tables[table_number - 1]
        if len(selected) > MAX_ROWS:
            raise ValueError("PDF table exceeds row limits")
        return normalize(((index, row) for index, row in enumerate(selected, 1)), require_cells=True)


def pdf_rows(data, page_number, table_number):
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "_pdf_worker", str(page_number), str(table_number)],
            input=data, capture_output=True, timeout=PDF_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("PDF table parsing timed out") from exc
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(error[:300] or "PDF table parser failed")
    if len(result.stdout) > 16 * 1024 * 1024:
        raise ValueError("PDF result exceeds output size")
    return json.loads(result.stdout)


def normalize(rows, require_cells=False):
    normalized = []
    cells = size = 0
    for index, row in rows:
        cells += len(row)
        if len(normalized) >= MAX_ROWS or len(row) > MAX_COLS or cells > MAX_CELLS:
            raise ValueError("table exceeds cell limits")
        if require_cells and any(cell is None for cell in row):
            raise ValueError("PDF table has missing geometry or merged cells")
        values = [cell_text(value) for value in row]
        size += sum(len(value.encode("utf-8")) for value in values)
        if size > MAX_TABLE_TEXT:
            raise ValueError("table exceeds text size limit")
        normalized.append((index, values))
    return normalized


def extract(raw_path, sheet=None, page=None, table=None, header=1, *, literal_text_column=None):
    path, data = local_bytes(raw_path)
    source = {"file": path.name, "header_row": header}
    suffix = path.suffix.lower()
    if literal_text_column is not None and suffix != ".xlsx":
        raise ValueError("literal text column validation requires an XLSX worksheet")
    if suffix in {".xlsx", ".docx"}:
        validate_ooxml(data)
    if suffix == ".xlsx" and sheet and page is None and table is None:
        source.update(format="xlsx", sheet=sheet)
        raw = normalize(xlsx_rows(data, sheet, header=header, literal_text_column=literal_text_column))
        if literal_text_column is not None:
            source.update(literal_text_column=literal_text_column, sha256=hashlib.sha256(data).hexdigest())
    elif suffix == ".docx" and table and sheet is None and page is None:
        source.update(format="docx", table=table)
        raw = normalize(docx_rows(data, table))
    elif suffix == ".pdf" and page and table and sheet is None:
        source.update(format="pdf", page=page, table=table)
        raw = pdf_rows(data, page, table)
    else:
        raise ValueError("select --sheet for XLSX, --table for DOCX, or --page and --table for PDF")
    rows = [(index, row) for index, row in raw if index >= header]
    if not rows or rows[0][0] != header:
        raise ValueError("header row is absent")
    columns = rows[0][1]
    # Workbooks often retain styles in trailing empty columns. Any data beyond
    # the final header is still rejected below.
    while columns and not columns[-1]:
        columns.pop()
    if not columns or any(not column for column in columns) or len(set(columns)) != len(columns):
        raise ValueError("headers must be nonempty and unique")
    records = []
    for index, row in rows[1:]:
        if not any(row):
            continue
        if len(row) < len(columns) or any(row[len(columns):]):
            raise ValueError(f"source row {index} does not match the header width")
        records.append({"source_row": index, "cells": row[:len(columns)]})
    if not records:
        raise ValueError("selected document table has no records")
    return {"source": source, "columns": columns, "rows": records}


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "_pdf_worker":
    try:
        data = sys.stdin.buffer.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError("PDF input exceeds file size limit")
        encoded = json.dumps(pdf_worker(int(sys.argv[2]), int(sys.argv[3]), data), ensure_ascii=False).encode()
        if len(encoded) > 16 * 1024 * 1024:
            raise ValueError("PDF result exceeds output size")
        sys.stdout.buffer.write(encoded)
    except Exception as exc:
        print(f"PDF read failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
