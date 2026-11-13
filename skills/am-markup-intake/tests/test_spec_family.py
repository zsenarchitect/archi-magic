import json
from pathlib import Path
import subprocess
import sys

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "spec_family_cli.py"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def write_json(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def draw_table(writer, rows):
    page = writer.add_blank_page(width=300, height=400)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    top = 360
    bottom = top - 24 * len(rows)
    lines = [f"{x} {bottom} m {x} {top} l S" for x in (10, 125, 250)]
    lines += [f"10 {y} m 250 {y} l S" for y in range(bottom, top + 1, 24)]
    for i, row in enumerate(rows):
        for x, value in zip((20, 135), row):
            lines.append(f"BT /F1 10 Tf {x} {top - 16 - 24*i} Td ({value}) Tj ET")
    content = DecodedStreamObject()
    content.set_data("\n".join(lines).encode())
    page[NameObject("/Contents")] = writer._add_object(content)


@pytest.fixture
def spec(tmp_path):
    pdf = tmp_path / "Equipment.pdf"
    mapping_path = tmp_path / "mapping.json"
    preview = tmp_path / "preview.json"
    decisions = tmp_path / "decisions.json"
    writer = PdfWriter()
    draw_table(writer, [("Dimension", "Value"), ("Width", "24 in"), ("Depth cm", "45"),
                        ("Height", "0.6 m")])
    draw_table(writer, [("Property", "Value"), ("Service connection", "DN25"),
                        ("Product code", "Demo-1")])
    writer.write(pdf)
    mapping = {"product": "Example unit", "variant": "Demo-1", "fields": [
        {"name": name, "page": page, "table": 1, "row": row, "column": 2}
        for name, page, row in (
            ("width", 1, 2), ("depth", 1, 3), ("height", 1, 4),
            ("connector_service", 2, 2), ("parameter_product_code", 2, 3),
        )
    ]}
    mapping["fields"][1]["unit"] = "cm"
    write_json(mapping_path, mapping)
    return pdf, mapping_path, preview, decisions


def test_real_pdf_preview_confirmation_and_stale_input(spec):
    pdf, mapping, preview, decisions = spec
    result = run("preview", pdf, "--mapping", mapping)
    assert result.returncode == 0, result.stderr
    preview.write_text(result.stdout)
    draft = json.loads(result.stdout)
    assert draft["kind"] == "spec-family-preview" and draft["status"] == "needs_confirmation"
    assert [(r["source"]["page"], r["extracted_text"], r["value"]) for r in draft["rows"]] == [
        (1, "24 in", "609.6"), (1, "45", "450"), (1, "0.6 m", "600"),
        (2, "DN25", "DN25"), (2, "Demo-1", "Demo-1"),
    ]

    result = run("template", "--preview", preview)
    assert result.returncode == 0, result.stderr
    decisions.write_text(result.stdout)
    confirm_args = ("confirm", pdf, "--mapping", mapping, "--preview", preview, "--decisions", decisions)
    assert run(*confirm_args).returncode == 2
    completed = json.loads(result.stdout)
    for choice in completed["decisions"]:
        choice["action"] = "accept"
    write_json(decisions, completed)

    reviewed = run(*confirm_args)
    assert reviewed.returncode == 0, reviewed.stderr
    report = json.loads(reviewed.stdout)
    assert report["model_write_approved"] is False
    assert report["dimensions_mm"] == {"width": "609.6", "depth": "450", "height": "600"}
    assert report["pdf"]["file"] == "Equipment.pdf"
    assert report["connectors"] == {"connector_service": "DN25"}
    assert report["source_rows"] == draft["rows"]

    original = pdf.read_bytes()
    changed = original.replace(b"Demo-1", b"Demo-2")
    assert changed != original and len(changed) == len(original)
    pdf.write_bytes(changed)
    stale = run(*confirm_args)
    assert stale.returncode == 2 and not stale.stdout and "has changed" in stale.stderr
    pdf.write_bytes(original)
    preview.write_text(preview.read_text() + " ")
    stale = run(*confirm_args)
    assert stale.returncode == 2 and not stale.stdout and "exact preview bytes" in stale.stderr


def test_confirms_fractional_dimensions_from_pdf_cells(spec):
    pdf, mapping, preview, decisions = spec
    def write_specification(width):
        writer = PdfWriter()
        draw_table(writer, [("Dimension", "Value"), ("Width", width), ("Depth", "3/4 ft"),
                            ("Height", "1 1/8 in")])
        draw_table(writer, [("Property", "Value"), ("Service connection", "DN25"), ("Product code", "Demo-1")])
        writer.write(pdf)

    write_specification('24-1/2"')
    mapping_data = json.loads(mapping.read_text())
    mapping_data["fields"][1]["unit"] = "ft"
    write_json(mapping, mapping_data)

    result = run("preview", pdf, "--mapping", mapping)
    assert result.returncode == 0, result.stderr
    preview.write_text(result.stdout)
    assert [row["extracted_text"] for row in json.loads(result.stdout)["rows"][:3]] == [
        '24-1/2"', "3/4 ft", "1 1/8 in",
    ]
    draft = run("template", "--preview", preview)
    assert draft.returncode == 0, draft.stderr
    reviewed = json.loads(draft.stdout)
    for choice in reviewed["decisions"]:
        choice["action"] = "accept"
    write_json(decisions, reviewed)

    result = run("confirm", pdf, "--mapping", mapping, "--preview", preview, "--decisions", decisions)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dimensions_mm"] == {
        "width": "622.3", "depth": "228.6", "height": "28.575",
    }

    for invalid_width in ('24--1/2"', "1/0 in", "24 3/2 in"):
        write_specification(invalid_width)
        failed = run("preview", pdf, "--mapping", mapping)
        assert failed.returncode == 2 and not failed.stdout and "width:" in failed.stderr


@pytest.mark.parametrize("change,message", [
    ("missing_unit", "explicit source unit"),
    ("conflict", "conflicts"),
    ("partial_clearance", "all six"),
    ("duplicate", "distinct"),
])
def test_invalid_mapping_has_no_preview(spec, change, message):
    pdf, mapping_path, *_ = spec
    mapping = json.loads(mapping_path.read_text())
    if change == "missing_unit":
        del mapping["fields"][1]["unit"]
    elif change == "conflict":
        mapping["fields"][0]["unit"] = "mm"
    else:
        new = {**mapping["fields"][0]}
        if change == "partial_clearance":
            new["name"] = "clearance_left"
        mapping["fields"].append(new)
    write_json(mapping_path, mapping)
    result = run("preview", pdf, "--mapping", mapping_path)
    assert result.returncode == 2 and not result.stdout and message in result.stderr


def test_directional_clearances_remain_separate(spec):
    pdf, mapping_path, *_ = spec
    mapping = json.loads(mapping_path.read_text())
    for index, side in enumerate(("left", "right", "front", "back", "below", "above")):
        mapping["fields"].append({**mapping["fields"][index % 3], "name": "clearance_" + side})
    write_json(mapping_path, mapping)
    result = run("preview", pdf, "--mapping", mapping_path)
    assert result.returncode == 0, result.stderr
    assert [(r["name"], r["value"]) for r in json.loads(result.stdout)["rows"][-6:]] == [
        ("clearance_left", "609.6"), ("clearance_right", "450"), ("clearance_front", "600"),
        ("clearance_back", "609.6"), ("clearance_below", "450"), ("clearance_above", "600"),
    ]
