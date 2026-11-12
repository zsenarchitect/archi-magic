"""Exercise the private DOCX comparison command with synthetic Office packages."""

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "bep_document_cli.py"
NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CURRENT = r"C:\Working\Tower.rvt"
CENTRAL = r"\\Server\Team New\Tower.rvt"
LINK = r"C:\Links\Structure.rvt"


@pytest.fixture
def facts(tmp_path):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps({
        "kind": "revit-model-facts", "status": "complete", "issues": [],
        "model": {"title": "Tower", "current_path": CURRENT, "central_path": CENTRAL,
                  "cloud_path": None, "workshared": True, "revit_year": "2026",
                  "revit_build": "20260215_1515(x64)"},
        "worksets": {"total": 8, "user": 2},
        "links": [{"id": "10000000000000001", "resolved_path": LINK, "status": "Loaded",
                   "stored_path": r"links\Structure.rvt", "provider_session_path": None}],
    }), encoding="utf-8")
    return path


def invoke(*args):
    result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                            check=False, capture_output=True, text=True)
    return result.returncode, json.loads(result.stdout)


def rewrite(path, replacement):
    with ZipFile(path) as source:
        parts = {name: source.read(name) for name in source.namelist()}
    parts.update(replacement)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as target:
        for name, data in parts.items():
            target.writestr(name, data)


def test_compares_exact_roles_and_old_names_with_evidence(tmp_path, facts):
    path = tmp_path / "BEP.docx"
    document = Document()
    document.add_paragraph(r"Central model: \\server\Team Old\Old Tower.rvt")
    document.add_paragraph(r"Folder only: \\Server\Team Old\Tower.rvt")
    document.add_paragraph("Retired Tower.rvt")
    document.add_paragraph('Consultant sent "Unverified Consultant.rvt".')
    document.add_paragraph("Current: c:/working/TOWER.rvt")
    document.add_table(rows=1, cols=1).cell(0, 0).text = LINK
    document.save(path)
    args = [path, "--facts", facts, "--former-host", "Retired Tower.rvt",
            "--field", "body/block:1=central", "--field", "body/block:2=central"]
    status, result = invoke(*args)
    assert status == 0
    assert result["status"] == "complete"
    assert result["skipped_counts"] == {}
    rows = result["references"]
    assert [(row["location"], row["status"]) for row in rows] == [
        ("body/block:1", "mismatch"), ("body/block:2", "mismatch"), ("body/block:3", "mismatch"),
        ("body/block:4", "unresolved"), ("body/block:5", "current_host"),
        ("body/block:6/row:1/cell:1/block:2", "current_link"),
    ]
    assert rows[0]["differences"] == ["file_name", "folder"]
    assert rows[0]["evidence"] == "reviewed_field"
    assert rows[0]["excerpt"] == r"Central model: \\server\Team Old\Old Tower.rvt"
    assert rows[1]["differences"] == ["folder"]
    assert rows[2]["evidence"] == "confirmed_former_alias"
    assert rows[3]["reason"] == "unrecognized_reference"
    assert rows[4]["host_roles"] == ["current"]
    assert rows[5]["link_id"] == "10000000000000001"
    assert invoke(*args) == (status, result)


def test_shared_header_and_footer_scanned_once(tmp_path, facts):
    document = Document()
    document.sections[0].header.paragraphs[0].text = CURRENT
    document.sections[0].footer.paragraphs[0].text = CENTRAL
    document.add_section()
    old_section, new_section = list(document.element.body.iter(qn("w:sectPr")))
    new_section.append(deepcopy(old_section.find(qn("w:headerReference"))))
    path = tmp_path / "shared.docx"
    document.save(path)
    code, result = invoke(path, "--facts", facts)
    assert code == 0
    assert result["status"] == "complete"
    assert [(row["location"], row["host_roles"]) for row in result["references"]] == [
        ("header:word/header1.xml/block:1", ["current"]),
        ("footer:word/footer1.xml/block:1", ["central"]),
    ]


def test_incomplete_coverage_never_confirms_a_partly_unscanned_field(tmp_path, facts):
    document = Document()
    paragraph = document.add_paragraph(r"Current: C:\Working\Wrong.rvt")
    box = OxmlElement("w:txbxContent")
    box_paragraph = OxmlElement("w:p")
    box.append(box_paragraph)
    paragraph.runs[0]._r.append(box)
    revision = OxmlElement("w:ins")
    paragraph._p.append(revision)
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "Hidden by merged cell: Another.rvt"
    outer = document.add_table(rows=1, cols=1)
    outer.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = CURRENT
    path = tmp_path / "unsupported.docx"
    document.save(path)
    code, result = invoke(path, "--facts", facts, "--field", "body/block:1=current")
    assert code == 0
    assert result["status"] == "incomplete"
    assert result["reference_status"] == "none_found"
    assert result["references"] == []
    assert result["skipped_counts"] == {
        "merged_table_cell": 1, "nested_table": 1, "reviewed_field_partly_unscanned": 1,
        "text_box": 1, "tracked_revision": 1,
    }


def test_empty_docx_and_invalid_facts_are_distinct(tmp_path, facts):
    path = tmp_path / "empty.docx"
    Document().save(path)
    assert invoke() == (0, {"status": "not_run", "reason": "no_docx"})
    assert invoke(path, "--facts", facts)[1]["reference_status"] == "none_found"
    facts.write_text('{"status":"incomplete","model":{},"links":[]}', encoding="utf-8")
    assert invoke(path, "--facts", facts) == (
        2, {"status": "rejected", "error": "choose a complete model-facts JSON export"})
    facts.write_text('{"status":"complete"', encoding="utf-8")
    code, result = invoke(path, "--facts", facts)
    assert code == 2
    assert result["status"] == "rejected"


@pytest.mark.parametrize("xml,expected", [
    ("<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val='15000'/></w:tcPr>"
     "<w:p/></w:tc></w:tr></w:tbl>", "DOCX grid span limit exceeded"),
    ("<w:p/>" * 5001, "DOCX physical p limit exceeded"),
    ("<w:tbl/>" * 101, "DOCX physical tbl limit exceeded"),
    ("<w:tbl>" + "<w:tr/>" * 1001 + "</w:tbl>", "DOCX physical tr limit exceeded"),
    ("<w:tbl><w:tr>" + "<w:tc/>" * 5001 + "</w:tr></w:tbl>", "DOCX physical tc limit exceeded"),
])
def test_physical_caps_refuse_before_text_traversal(tmp_path, facts, xml, expected):
    path = tmp_path / "excess.docx"
    Document().save(path)
    main = f"<w:document {NS}><w:body>{xml}</w:body></w:document>"
    rewrite(path, {"word/document.xml": main})
    assert path.stat().st_size < 50000
    assert invoke(path, "--facts", facts) == (2, {"status": "rejected", "error": expected})


def test_distinct_header_footer_parts_are_capped(tmp_path, facts):
    path = tmp_path / "excess-headers.docx"
    Document().save(path)
    prefix = "http://schemas.openxmlformats.org/"
    refs = "".join(f'<w:headerReference r:id="rId{i}"/>' for i in range(33))
    rels = "".join(f'<Relationship Id="rId{i}" Type="{prefix}officeDocument/2006/relationships/header"'
                   f' Target="extra{i}.xml"/>' for i in range(33))
    entries = {f"word/extra{i}.xml": f"<w:hdr {NS}><w:p/></w:hdr>" for i in range(33)}
    entries["word/document.xml"] = (
        f'<w:document {NS} xmlns:r="{prefix}officeDocument/2006/relationships">'
        f"<w:body><w:sectPr>{refs}</w:sectPr></w:body></w:document>")
    entries["word/_rels/document.xml.rels"] = (
        f'<Relationships xmlns="{prefix}package/2006/relationships">{rels}</Relationships>')
    rewrite(path, entries)
    assert invoke(path, "--facts", facts) == (
        2, {"status": "rejected", "error": "DOCX header and footer part limit exceeded"})
