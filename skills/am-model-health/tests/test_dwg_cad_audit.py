import json
import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from dwg_cad_audit import (
    check_layer_standards,
    check_xrefs,
    check_blocks_and_attributes,
    check_scale_and_units,
    run_cad_audit,
)


@pytest.fixture
def clean_cad_drawing():
    return {
        "filename": "A-101-FloorPlan.dwg",
        "header": {
            "$INSUNITS": 1,      # Inches
            "$MEASUREMENT": 0,   # English
        },
        "layers": [
            {"name": "A-WALL", "entityCount": 150},
            {"name": "A-DOOR", "entityCount": 42},
            {"name": "A-GLAZ", "entityCount": 28},
            {"name": "A-ANNO-DIMS", "entityCount": 95},
        ],
        "xrefs": [
            {"name": "Civil-Background", "path": ".\\xrefs\\Civil-Background.dwg", "status": "loaded", "isCircular": False},
        ],
        "blocks": [
            {
                "name": "Door-Single-36x84",
                "count": 42,
                "attributes": [{"tag": "MARK", "value": "101", "isRequired": True}],
            }
        ],
    }


@pytest.fixture
def non_compliant_cad_drawing():
    return {
        "filename": "DirtyPlan.dwg",
        "header": {
            "$INSUNITS": 0,      # Unspecified units (Error)
            "$MEASUREMENT": 0,
        },
        "layers": [
            {"name": "0", "entityCount": 85},                # Drawing on layer 0
            {"name": "MyCustomWallLayer", "entityCount": 20}, # Non-standard discipline
            {"name": "A-INVALIDGROUP", "entityCount": 5},     # Non-standard major group
        ],
        "xrefs": [
            {"name": "OldStructure", "path": "Z:\\OfficeNetwork\\Project\\OldStructure.dwg", "status": "missing", "isCircular": False},
            {"name": "CircularPlan", "path": ".\\CircularPlan.dwg", "status": "loaded", "isCircular": True},
        ],
        "blocks": [
            {"name": "*U14", "count": 12},  # Anonymous block
            {
                "name": "RoomTag",
                "count": 10,
                "attributes": [{"tag": "ROOM_NUM", "value": "", "isRequired": True}], # Blank required attr
            },
        ],
    }


def test_clean_cad_drawing_produces_zero_errors(clean_cad_drawing):
    result = run_cad_audit(clean_cad_drawing)
    assert result["total_layers"] == 4
    assert result["total_xrefs"] == 1
    assert result["summary"]["errors"] == 0
    assert result["summary"]["warnings"] == 0


def test_non_compliant_drawing_reports_all_categories(non_compliant_cad_drawing):
    result = run_cad_audit(non_compliant_cad_drawing)
    assert result["total_findings"] > 0

    rule_ids = [f["ruleId"] for f in result["findings"]]
    assert "CAD-LAY-01" in rule_ids
    assert "CAD-XREF-01" in rule_ids
    assert "CAD-BLK-01" in rule_ids
    assert "CAD-UNIT-01" in rule_ids

    # Unresolved xref error & circular xref error
    xref_errors = [f for f in result["findings"] if f["ruleId"] == "CAD-XREF-01" and f["severity"] == "error"]
    assert len(xref_errors) >= 2

    # $INSUNITS 0 is an error
    unit_errors = [f for f in result["findings"] if f["ruleId"] == "CAD-UNIT-01" and f["severity"] == "error"]
    assert len(unit_errors) == 1
    assert "$INSUNITS" in unit_errors[0]["target"]


def test_cli_execution_with_output(tmp_path, non_compliant_cad_drawing):
    in_file = tmp_path / "cad_in.json"
    out_file = tmp_path / "cad_out.json"

    with open(in_file, "w") as f:
        json.dump(non_compliant_cad_drawing, f)

    script = os.path.join(HERE, "..", "scripts", "dwg_cad_audit.py")
    ret = os.system(f'python "{script}" "{in_file}" --output "{out_file}"')
    assert os.path.exists(out_file)

    with open(out_file, "r") as f:
        data = json.load(f)
    assert data["total_findings"] > 0
    assert data["summary"]["errors"] > 0


@pytest.mark.parametrize("raw", [None, True, 2.9, "2.9", 25])
def test_invalid_insertion_unit_is_unverifiable_against_expectation(raw):
    findings = check_scale_and_units({"$INSUNITS": raw}, {"$INSUNITS": 2})
    assert len(findings) == 1
    assert findings[0]["target"] == "$INSUNITS"
    assert findings[0]["code"] is None and findings[0]["expected_code"] == 2
    assert "could not be verified" in findings[0]["message"]
