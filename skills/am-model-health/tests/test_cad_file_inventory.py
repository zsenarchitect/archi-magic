import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import ezdxf
from ezdxf import xref
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from cad_file_inventory import dwg_inventory, read_xref_path, run_decoder, validate_json
from dwg_cad_audit import audit_file


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "dwg_cad_audit.py"), *map(str, args)],
        capture_output=True, text=True,
    )


def test_direct_dxf_reports_seeded_findings_and_inventory(tmp_path):
    source = tmp_path / "audit.dxf"
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    doc.layers.new("NonStandard", dxfattribs={"color": 3})
    model = doc.modelspace()
    model.add_line((0, 0), (1, 0), dxfattribs={"layer": "NonStandard"})
    xref.define(doc, "Missing", "xref/missing.dwg")
    model.add_blockref("Missing", (0, 0))
    block = doc.blocks.new("Title")
    block.add_attdef("MARK", (0, 0), text="Default")
    insert = model.add_blockref("Title", (2, 1))
    insert.add_attrib("MARK", "Actual")
    doc.saveas(source)
    original = source.read_bytes()
    result = run(source)
    assert result.returncode == 3, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "incomplete"
    assert ("CAD-LAY-01", "NonStandard") in {(f["ruleId"], f["target"]) for f in report["findings"]}
    assert ("CAD-XREF-01", "Missing") in {(f["ruleId"], f["target"]) for f in report["findings"]}
    drawing = report["inventory"]
    assert drawing["sha256"] == hashlib.sha256(original).hexdigest()
    assert drawing["xrefs"] == [{"name": "Missing", "path": "xref/missing.dwg",
                                 "flags_consistent": True, "status": "missing", "isCircular": False}]
    title = next(b for b in drawing["blocks"] if b["name"] == "Title")
    assert title["count"] == 1
    assert title["insert_handles"] == [insert.dxf.handle]
    assert title["definitions"] == [{"tag": "MARK", "value": "Default", "constant": False}]
    assert title["attributes"] == [{"tag": "MARK", "value": "Actual", "insert_handle": insert.dxf.handle}]
    assert source.read_bytes() == original
    refused = run(source, "--output", source)
    assert refused.returncode == 2 and source.read_bytes() == original


def test_path_checks_do_not_follow_intermediate_symlinks(tmp_path):
    source = tmp_path / "root.dwg"
    source.write_bytes(b"drawing")
    nested = tmp_path / "real"
    nested.mkdir()
    (nested / "local.dwg").touch()
    (tmp_path / "alias").symlink_to(nested, target_is_directory=True)
    assert read_xref_path("real\\local.dwg", source) == ("present", False)
    assert read_xref_path("real/missing.dwg", source) == ("missing", False)
    assert read_xref_path("./root.dwg", source) == ("present", True)
    for path in ("C:other.dwg", r"\\host\share\other.dwg", "../other.dwg",
                 "alias/local.dwg", r"\root.dwg", "real/local.dwg:stream"):
        assert read_xref_path(path, source) == ("unchecked", False)


def test_dwg_inventory_uses_absolute_handles_and_retains_orphans():
    data = {
        "FILEHEADER": {"version": "AC1015"}, "created_by": "LibreDWG 0.14",
        "HEADER": {"INSUNITS": 4}, "Template": {"MEASUREMENT": 1},
        "OBJECTS": [
            {"object": "LAYER", "handle": [0, 1, 10], "name": "A-WALL", "color": 3},
            {"entity": "BLOCK", "handle": [0, 1, 32], "layer": [5, 1, 10, 10]},
            {"entity": "ENDBLK", "handle": [0, 1, 33], "layer": [5, 1, 10, 10]},
            {"entity": "SEQEND", "handle": [0, 1, 34], "layer": [5, 1, 10, 10]},
            {"object": "BLOCK_HEADER", "handle": [0, 1, 30], "name": "Title", "is_xref_ref": 1},
            {"object": "BLOCK_HEADER", "handle": [0, 1, 31], "name": "External", "blkisxref": 1,
             "xref_pname": "local.dwg"},
            {"entity": "INSERT", "handle": [0, 1, 20], "layer": [12, 1, 9, 10],
             "block_header": [12, 1, 9, 30]},
            {"entity": "ATTDEF", "handle": [0, 1, 21], "tag": "MARK", "default_value": "Default",
             "ownerhandle": [12, 1, 9, 30]},
            {"entity": "ATTRIB", "handle": [0, 1, 22], "tag": "MARK", "text_value": "Actual",
             "ownerhandle": [12, 1, 9, 20]},
            {"entity": "ATTRIB", "handle": [0, 1, 23], "tag": "OTHER", "text_value": "Unassigned"},
        ],
    }
    result = dwg_inventory(data, "AC1015")
    assert result["header"] == {"$INSUNITS": 4, "$MEASUREMENT": 1}
    assert result["layers"][0]["entityCount"] == 1
    assert result["xrefs"] == [{"name": "External", "path": "local.dwg", "flags_consistent": True}]
    assert result["blocks"] == [{
        "name": "Title", "count": 1, "insert_handles": [20],
        "attributes": [{"handle": 22, "tag": "MARK", "value": "Actual", "insert_handle": 20}],
        "definitions": [{"handle": 21, "tag": "MARK", "value": "Default", "insert_handle": None, "constant": None}],
    }]
    assert result["unassociated_attributes"] == [{"handle": 23, "tag": "OTHER", "value": "Unassigned"}]
    assert result["notes"]


def test_native_failed_decode_never_publishes_report(tmp_path, monkeypatch):
    source = tmp_path / "bad.dwg"
    source.write_bytes(b"AC1015fixture")
    output = tmp_path / "out.json"
    decoder = Path(sys.executable)
    monkeypatch.setattr("cad_file_inventory.run_decoder", lambda *_: (b"{}", b""))
    assert audit_file(SimpleNamespace(input=str(source), dwgread=str(decoder),
                                      output=str(output), summary=False)) == 2
    assert not output.exists()


def test_bounds_and_legacy_inventory(tmp_path):
    assert validate_json(b'{"type":1,"type":2}', dwg=True) == {"type": 2}
    with pytest.raises(ValueError, match="repeated"):
        validate_json(b'{"name":"first","name":"second"}', dwg=True)
    with pytest.raises(ValueError, match="output limit"):
        run_decoder([sys.executable, "-c", "import sys; sys.stderr.write('x' * 300000)"], tmp_path)
    with pytest.raises(ValueError, match="timed out"):
        run_decoder([sys.executable, "-c", "import time; time.sleep(3)"], tmp_path, timeout=0.15)
    source = tmp_path / "inventory.data"
    source.write_text(json.dumps({"filename": "Legacy.dwg", "layers": [], "xrefs": [], "blocks": []}))
    output = tmp_path / "report.json"
    output.write_text("old")
    assert run(source, "--output", output, "--summary").returncode == 0
    assert json.loads(output.read_text())["drawing_name"] == "Legacy.dwg"


def test_dxf_purge_review_excludes_indirect_and_protected_blocks(tmp_path):
    source = tmp_path / "block-review.dxf"
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    for name in ("Unused", "Nested", "Parent", "Paper", "Arrow", "ExtendedData", "ExternalDependent"):
        doc.blocks.new(name)
    doc.blocks.new("*U1", dxfattribs={"flags": 1})
    doc.blocks.get("ExternalDependent").block.dxf.flags = 16
    doc.blocks.get("Parent").add_blockref("Nested", (0, 0))
    doc.modelspace().add_blockref("Parent", (1, 1))
    doc.paperspace().add_blockref("Paper", (1, 1))
    doc.header["$DIMBLK"] = "Arrow"
    doc.appids.new("REVIEW_FIXTURE")
    line = doc.modelspace().add_line((0, 0), (1, 0))
    line.set_xdata("REVIEW_FIXTURE", [(1005, doc.blocks.get("ExtendedData").block_record_handle)])
    doc.saveas(source)
    original = source.read_bytes()

    result = run(source)
    assert result.returncode == 0, result.stderr
    inventory = json.loads(result.stdout)["inventory"]
    assert inventory["coverage"]["block_purge_review"] == "known_dxf_references"
    assert inventory["coverage"]["purge_eligibility"] == "not_assessed"
    assert inventory["purge_candidates"] == [{"kind": "block", "name": "Unused"}]
    assert source.read_bytes() == original


def test_dxf_layer_policy_checks_linetype_aci_and_true_color_without_edits(tmp_path):
    source, policy = tmp_path / "drawing.dxf", tmp_path / "standards.json"
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    layer = doc.layers.new("A-WALL", dxfattribs={"color": 3})
    layer.off()
    doc.layers.new("A-DOOR", dxfattribs={"color": 3, "true_color": 0x225588})
    doc.layers.new("A-FLOR", dxfattribs={"color": 5})
    doc.saveas(source)
    original = source.read_bytes()
    policy.write_text(json.dumps({"layers": {
        "a-wall": {"aci": 3, "linetype": "continuous"},
        "A-DOOR": {"aci": 3},
        "A-FLOR": {"aci": 4, "linetype": "Dashed"},
        "A-ANNO": {"aci": 3},
    }}))
    result = run(source, "--layer-policy", policy)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    checks = [f for f in report["findings"] if f["ruleId"] == "CAD-LAY-02"]
    assert [(f["target"], f["field"], f["observed_field"], f["actual"]) for f in checks] == [
        ("A-DOOR", "aci", "rgb", "#225588"), ("A-FLOR", "aci", "aci", 5),
        ("A-FLOR", "linetype", "linetype", "Continuous"),
    ]
    evidence = report["inventory"]["layer_policy"]
    assert evidence == {"sha256": hashlib.sha256(policy.read_bytes()).hexdigest(), "checked_fields": 5,
                        "unchecked_fields": [], "unmatched_layers": ["A-ANNO"]}
    assert report["inventory"]["coverage"]["layer_style_policy"] == "checked"
    assert source.read_bytes() == original

    policy.write_text('{"layers":{"A-DOOR":{"rgb":"#225588"}}}')
    matches = json.loads(run(source, "--layer-policy", policy).stdout)
    assert not any(f["ruleId"] == "CAD-LAY-02" for f in matches["findings"])
    policy.write_text('{"layers":{"A-ANNO":{"aci":3}}}')
    absent = run(source, "--layer-policy", policy)
    assert absent.returncode == 3
    assert json.loads(absent.stdout)["inventory"]["coverage"]["layer_style_policy"] == "incomplete"


def test_layer_policy_keeps_unreadable_dwg_color_separate_from_linetype(tmp_path, monkeypatch):
    from dwg_cad_audit import check_layer_policy

    records = [
        {"object": "LAYER", "handle": [0, 1, 10], "name": "A-WALL",
         "color": {"index": 3, "rgb": "c3000003"}, "ltype": [12, 1, 9, 15]},
        {"object": "LTYPE", "handle": [0, 1, 15], "name": "Continuous"},
    ]
    native = {"FILEHEADER": {"version": "AC1032"}, "created_by": "LibreDWG 0.14", "OBJECTS": records,
              "HEADER": {"INSUNITS": 4}, "Template": {"MEASUREMENT": 1}}
    drawing = dwg_inventory(native, "AC1032")
    findings, evidence = check_layer_policy(drawing, {"a-wall": ("A-WALL", {"aci": 3, "linetype": "Dashed"})})
    assert [(f["field"], f["actual"]) for f in findings] == [("linetype", "Continuous")]
    assert evidence == {"checked_fields": 1, "unchecked_fields": [{"layer": "A-WALL", "field": "aci"}],
                        "unmatched_layers": []}

    source, policy, output = (tmp_path / name for name in ("sample.dwg", "policy.json", "report.json"))
    source.write_bytes(b"AC1032fixture")
    monkeypatch.setattr("cad_file_inventory.run_decoder", lambda *_: (json.dumps(native).encode(), b""))
    policy.write_text('{"layers":{"A-WALL":{"aci":3,"linetype":"Continuous"}}}')
    assert audit_file(SimpleNamespace(input=str(source), dwgread=sys.executable, output=str(output),
                                      summary=False, layer_policy=str(policy))) == 3
    assert json.loads(output.read_text())["inventory"]["coverage"]["layer_style_policy"] == "incomplete"


def test_layer_policy_rejects_duplicate_names_and_conflicting_color_modes(tmp_path):
    from dwg_cad_audit import read_layer_policy

    policy = tmp_path / "bad.json"
    for raw in ('{"layers":{"A-WALL":{"aci":3},"a-wall":{"aci":4}}}',
                '{"layers":{"A-WALL":{"aci":3,"rgb":"#225588"}}}',
                '{"layers":{"A-WALL":{"aci":3,"aci":4}}}'):
        policy.write_text(raw)
        with pytest.raises(ValueError):
            read_layer_policy(str(policy))


def test_dxf_required_attributes_are_checked_per_insert(tmp_path):
    source, policy = tmp_path / "drawing.dxf", tmp_path / "title-tags.json"
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.header["$MEASUREMENT"] = 1
    block = doc.blocks.new("Title")
    block.add_attdef("SHEET", text="Generic default")
    block.add_attdef("OFFICE", text="Filled constant", dxfattribs={"flags": 2})
    model = doc.modelspace()
    complete = model.add_blockref("Title", (0, 0))
    complete.add_attrib("Sheet", "A101")
    missing = doc.paperspace().add_blockref("Title", (1, 0))
    blank = model.add_blockref("Title", (2, 0))
    blank.add_attrib("SHEET", " ")
    duplicate = model.add_blockref("Title", (3, 0))
    duplicate.add_attrib("sheet", "A102")
    duplicate.add_attrib("SHEET", "A103")
    doc.saveas(source)
    original = source.read_bytes()
    policy.write_text('{"blocks":{"title":["SHEET","OFFICE"],"Unplaced":["TAG"]}}')

    result = run(source, "--attribute-policy", policy)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    findings = [f for f in report["findings"] if f["ruleId"] == "CAD-BLK-02"]
    assert {(f["insert_handle"], f["tag"], f["state"]) for f in findings} == {
        (missing.dxf.handle, "SHEET", "missing"), (blank.dxf.handle, "SHEET", "blank"),
        (duplicate.dxf.handle, "SHEET", "duplicate"),
    }
    assert report["inventory"]["attribute_policy"] == {
        "sha256": hashlib.sha256(policy.read_bytes()).hexdigest(), "checked_fields": 8,
        "unchecked_fields": [], "unmatched_blocks": ["Unplaced"],
    }
    assert report["inventory"]["coverage"]["required_attribute_policy"] == "checked"
    assert source.read_bytes() == original


def test_dwg_policy_marks_unknown_attribute_values_unchecked():
    from dwg_cad_audit import check_attribute_policy

    data = {"FILEHEADER": {"version": "AC1032"}, "created_by": "LibreDWG 0.14",
            "HEADER": {"INSUNITS": 4}, "Template": {"MEASUREMENT": 1}, "OBJECTS": [
                {"object": "BLOCK_HEADER", "handle": [0, 1, 30], "name": "Title"},
                {"entity": "MINSERT", "handle": [0, 1, 20], "block_header": [12, 1, 9, 30]},
                {"entity": "ATTDEF", "handle": [0, 1, 21], "tag": "OFFICE", "default_value": "Value",
                 "ownerhandle": [12, 1, 9, 30], "flags": 2},
                {"entity": "ATTRIB", "handle": [0, 1, 22], "tag": "SHEET", "text_value": "uncertain fallback",
                 "ownerhandle": [12, 1, 9, 20], "mtext_type": 2},
            ]}
    drawing = dwg_inventory(data, "AC1032")
    findings, evidence = check_attribute_policy(drawing, {"title": ("Title", ["OFFICE", "SHEET", "DATE"])})
    assert [(f["insert_handle"], f["tag"], f["state"]) for f in findings] == [(20, "DATE", "missing")]
    assert evidence == {"checked_fields": 2, "unchecked_fields": [{"block": "Title", "insert_handle": 20,
                                                                  "tag": "SHEET"}], "unmatched_blocks": []}


def test_dxf_insertion_units_and_pattern_standard_are_checked_independently(tmp_path):
    source = tmp_path / "survey.dxf"
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 21
    doc.header["$MEASUREMENT"] = 1
    doc.saveas(source)
    original = source.read_bytes()

    ordinary = run(source, "--summary")
    assert ordinary.returncode == 0, ordinary.stderr
    summary = json.loads(ordinary.stdout)
    assert summary["status"] == "audited"
    assert summary["summary"] == {"errors": 0, "warnings": 0, "infos": 0}
    assert summary["units"]["$INSUNITS"] == {"code": 21, "label": "US Survey Feet",
                                            "expected_code": None, "expected_label": None}
    assert summary["units"]["$MEASUREMENT"]["label"] == "Metric"

    matched = run(source, "--expected-insunits", 21, "--expected-measurement", 1)
    assert matched.returncode == 0 and json.loads(matched.stdout)["findings"] == []
    mismatch = run(source, "--expected-insunits", 2, "--expected-measurement", 0)
    assert mismatch.returncode == 1, mismatch.stderr
    findings = json.loads(mismatch.stdout)["findings"]
    assert [(f["target"], f["severity"], f["code"], f["expected_code"]) for f in findings] == [
        ("$INSUNITS", "error", 21, 2), ("$MEASUREMENT", "warning", 1, 0),
    ]
    assert source.read_bytes() == original


def test_newer_dwg_unit_code_is_recognized_through_native_inventory(tmp_path, monkeypatch):
    source = tmp_path / "survey.dwg"
    source.write_bytes(b"AC1032fixture")
    decoded = {"FILEHEADER": {"version": "AC1032"}, "created_by": "LibreDWG 0.14",
               "HEADER": {"INSUNITS": 24}, "Template": {"MEASUREMENT": 0},
               "OBJECTS": [{"object": "LAYER", "handle": [0, 1, 10], "name": "A-WALL"}]}
    monkeypatch.setattr("cad_file_inventory.run_decoder", lambda *_: (json.dumps(decoded).encode(), b""))
    output = tmp_path / "report.json"
    assert audit_file(SimpleNamespace(input=str(source), dwgread=sys.executable, output=str(output),
                                      summary=False, expected_insunits=24)) == 0
    report = json.loads(output.read_text())
    assert report["units"]["$INSUNITS"]["label"] == "US Survey Miles"
    assert report["status"] == "audited"
