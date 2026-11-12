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
    assert title["definitions"] == [{"tag": "MARK", "value": "Default"}]
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
        "name": "Title", "count": 1,
        "attributes": [{"handle": 22, "tag": "MARK", "value": "Actual", "insert_handle": 20}],
        "definitions": [{"handle": 21, "tag": "MARK", "value": "Default", "insert_handle": None}],
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
