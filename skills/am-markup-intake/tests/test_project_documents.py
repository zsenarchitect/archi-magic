import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

from docx import Document
import openpyxl
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


SKILLS = Path(__file__).resolve().parents[2]
SCRIPT = SKILLS / "am-markup-intake/scripts/project_document_cli.py"
MODEL = SKILLS / "am-schedule-view/assets/fixtures/snowdon-schedules.sample.json"
SCHEDULE = "Room Finish Schedule"


def invoke(mode, path, *args):
    return subprocess.run([sys.executable, str(SCRIPT), mode, str(path), *map(str, args)],
                          capture_output=True, text=True, check=False)


def compare(path, snapshot=MODEL, *args):
    return invoke("compare", path, "--sheet", "Room Data", "--schedule-json", snapshot,
                  "--schedule", SCHEDULE, "--key", "Room=Room Number",
                  "--field", "Floor=Floor Finish", "--field", "Wall=Wall Finish", *args)


def workbook(path, rows):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Room Data"
    for row in rows:
        sheet.append(row)
    book.save(path)
    book.close()


def test_snowdon_real_workbook_comparison_with_exact_provenance(tmp_path):
    source = json.loads(MODEL.read_text())
    schedule = next(item for item in source["schedules"] if item["name"] == SCHEDULE)
    rows = [(row[0], row[3], row[5]) for row, kind in zip(schedule["rows"], schedule["rowKinds"])
            if kind == "body"]
    path = tmp_path / "Room data sheet.xlsx"
    workbook(path, [("Room", "Floor", "Wall"), *rows])
    result = compare(path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["counts"] == {"expected_rows": 54, "schedule_rows": 54,
                                "mismatched_cells": 0, "missing_in_schedule": 0, "actual_only_rows": 0}

    changed = [("P01", "Tile", "Paint"), *rows[2:], ("MISSING-001", "Carpet", "Paint")]
    workbook(path, [("Room", "Floor", "Wall"), *changed])
    result = compare(path)
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["document"] == {
        "file": "Room data sheet.xlsx", "sheet": "Room Data", "format": "xlsx", "header_row": 1,
    }
    assert report["schedule"]["schedule_id"] == 510937
    assert report["schedule"]["read_at"] == "2026-10-19 15:28"
    assert report["schedule"]["excluded_snapshot_rows"] == {"blank": 9, "group": 9, "total": 10}
    assert report["mismatches"] == [
        {"key": {"Room": "P01"}, "source_row": 2, "snapshot_row": 4, "document_column": "Floor",
         "schedule_column": "Floor Finish", "expected": "Tile", "actual": "Concrete"},
        {"key": {"Room": "P01"}, "source_row": 2, "snapshot_row": 4, "document_column": "Wall",
         "schedule_column": "Wall Finish", "expected": "Paint", "actual": "Concrete"},
    ]
    assert report["missing_in_schedule"] == [{"key": {"Room": "MISSING-001"}, "source_row": 55}]
    assert report["actual_only"] == [{"key": {"Room": "E1"}, "snapshot_row": 3}]


@pytest.mark.parametrize("shape", ["bare", "payload", "run", "run_bare"])
def test_schedule_shapes_keep_literals_and_composite_room_keys(tmp_path, shape):
    model = {"schedules": [{"name": SCHEDULE, "columns": ["Room Number", "Level", "Floor Finish", "Wall Finish"],
                            "rows": [["001", "A", r"C:\unit", ""], ["001", "B", "", "Paint"]],
                            "rowKinds": ["body", "body"], "itemized": True, "truncated": False}]}
    payload = {"payloadFormat": "ascii-json", "payload": json.dumps(model, ensure_ascii=True)}
    content = {"bare": model, "payload": payload, "run": {"result": payload},
               "run_bare": {"result": model}}[shape]
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(content))
    path = tmp_path / "Rooms.xlsx"
    workbook(path, [("Room", "Level", "Floor", "Wall"), ("001", "A", r"C:\unit", ""), ("001", "B", "", "Paint")])
    assert compare(path, snapshot).returncode == 2
    result = compare(path, snapshot, "--key", "Level=Level")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["counts"]["expected_rows"] == 2


@pytest.mark.parametrize("failure", ["truncated", "grouped", "missing_kinds", "unknown_kind", "missing_sheet"])
def test_incomplete_input_cannot_pass(tmp_path, failure):
    source = {"name": SCHEDULE, "columns": ["Room Number", "Floor Finish", "Wall Finish"],
              "rows": [["001", "", ""]], "rowKinds": ["body"], "itemized": True, "truncated": False}
    if failure == "truncated":
        source["truncated"] = True
    elif failure == "grouped":
        source["itemized"] = False
    elif failure == "missing_kinds":
        del source["rowKinds"]
    elif failure == "unknown_kind":
        source["rowKinds"] = ["unspecified"]
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"schedules": [source]}))
    path = tmp_path / "Rooms.xlsx"
    if failure == "missing_sheet":
        openpyxl.Workbook().save(path)
    else:
        workbook(path, [("Room", "Floor", "Wall"), ("001", "", "")])
    result = compare(path, snapshot)
    assert result.returncode == 2 and result.stdout == ""


def test_xlsx_formula_and_oversized_merge_fail_closed(tmp_path):
    path = tmp_path / "Rooms.xlsx"
    workbook(path, [("Room", "Floor"), ("001", "=2+2")])
    result = invoke("extract", path, "--sheet", "Room Data")
    assert result.returncode == 2 and "formula" in result.stderr

    malformed = tmp_path / "Malformed.xlsx"
    with ZipFile(path) as source, ZipFile(malformed, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                data = data.replace(b"</worksheet>", b'<mergeCells><mergeCell ref="A1:XFD1048576"/></mergeCells></worksheet>')
            target.writestr(name, data)
    result = invoke("extract", malformed, "--sheet", "Room Data")
    assert result.returncode == 2 and "bounds" in result.stderr


def test_docx_table_and_merge_detection(tmp_path):
    path = tmp_path / "Rooms.docx"
    document = Document()
    table = document.add_table(rows=2, cols=2)
    for row, values in zip(table.rows, [["Room", "Floor"], ["001", ""]]):
        for cell, value in zip(row.cells, values):
            cell.text = value
    document.save(path)
    result = invoke("extract", path, "--table", 1)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "source": {"file": "Rooms.docx", "header_row": 1, "format": "docx", "table": 1},
        "columns": ["Room", "Floor"], "rows": [{"source_row": 2, "cells": ["001", ""]}],
    }
    table.cell(1, 0).merge(table.cell(1, 1))
    document.save(path)
    result = invoke("extract", path, "--table", 1)
    assert result.returncode == 2 and "merged" in result.stderr


def test_vector_pdf_table_extracts_actual_page_and_table(tmp_path):
    path = tmp_path / "Rooms.pdf"
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    ops = []
    for x in (10, 100, 200):
        ops.append(f"{x} 150 m {x} 200 l S")
    for y in (150, 175, 200):
        ops.append(f"10 {y} m 200 {y} l S")
    for x, y, value in ((20, 185, "Room"), (120, 185, "Floor"), (20, 160, "001"), (120, 160, "Tile")):
        ops.append(f"BT /F1 10 Tf {x} {y} Td ({value}) Tj ET")
    content = DecodedStreamObject()
    content.set_data("\n".join(ops).encode())
    page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)
    result = invoke("extract", path, "--page", 1, "--table", 1)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "source": {"file": "Rooms.pdf", "header_row": 1, "format": "pdf", "page": 1, "table": 1},
        "columns": ["Room", "Floor"], "rows": [{"source_row": 2, "cells": ["001", "Tile"]}],
    }
    bad = invoke("extract", path, "--page", 2, "--table", 1)
    assert bad.returncode == 2 and bad.stdout == ""


@pytest.mark.parametrize("path", ["https://example.com/project.xlsx", r"\\server\share\project.xlsx"])
def test_does_not_open_remote_documents(path):
    result = invoke("extract", path, "--sheet", "Room Data")
    assert result.returncode == 2 and "local" in result.stderr and not result.stdout


def test_literal_sheet_number_column_preserves_text_and_hashes_exact_workbook(tmp_path):
    from hashlib import sha256

    path = tmp_path / "Drawing index.xlsx"
    workbook(path, [("Issue index", ""), ("Sheet Number", "Sheet Name"), ("001", "Cover")])
    before = path.read_bytes()
    report = invoke("extract", path, "--sheet", "Room Data", "--header-row", 2,
                    "--literal-text-column", "Sheet Number")
    assert report.returncode == 0, report.stderr
    result = json.loads(report.stdout)
    assert result["rows"] == [{"source_row": 3, "cells": ["001", "Cover"]}]
    assert result["source"]["sha256"] == sha256(before).hexdigest()
    assert result["source"]["literal_text_column"] == "Sheet Number"
    assert path.read_bytes() == before


@pytest.mark.parametrize("number", [1, " 001 ", None])
def test_literal_sheet_number_column_rejects_ambiguous_cells(tmp_path, number):
    path = tmp_path / "Drawing index.xlsx"
    workbook(path, [("Sheet Number", "Sheet Name"), (number, "Cover")])
    book = openpyxl.load_workbook(path)
    book.active["A2"].number_format = "000"
    book.save(path)
    book.close()
    before = path.read_bytes()
    report = invoke("extract", path, "--sheet", "Room Data", "--literal-text-column", "Sheet Number")
    assert report.returncode == 2 and "row 2" in report.stderr and "literal text" in report.stderr
    assert not report.stdout and path.read_bytes() == before
