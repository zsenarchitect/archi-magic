import json
import os
import sys
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

from mep_qa import (
    check_mep_system,
    check_open_connectors,
    check_sizing_and_flow,
    check_clearance_zones,
    run_mep_audit,
)


@pytest.fixture
def clean_mep_elements():
    return [
        {
            "id": 101,
            "category": "Ducts",
            "level": "Level 1",
            "location": {"x": 10.0, "y": 20.0, "z": 12.0},
            "parameters": {
                "System Type": "Supply Air",
                "System Name": "SA-01",
                "Width": 24.0,
                "Height": 12.0,
                "Flow": "1200 CFM",
            },
            "connectors": [
                {"isConnected": True, "domain": "Hvac", "origin": {"x": 10.0, "y": 20.0, "z": 12.0}},
                {"isConnected": True, "domain": "Hvac", "origin": {"x": 30.0, "y": 20.0, "z": 12.0}},
            ],
        },
        {
            "id": 102,
            "category": "Pipes",
            "level": "Level 1",
            "parameters": {
                "System Type": "Domestic Cold Water",
                "System Name": "CW-Main",
                "Size": '2 1/2"',
            },
            "connectors": [
                {"isConnected": True, "domain": "Piping"},
                {"isConnected": True, "domain": "Piping"},
            ],
        },
        {
            "id": 103,
            "category": "Mechanical Equipment",
            "name": "Air Handling Unit AHU-1",
            "level": "Roof",
            "parameters": {
                "System Type": "Supply Air",
                "System Name": "SA-01",
                "Clearance Zone": "36-inch front access",
            },
            "connectors": [],
        },
    ]


@pytest.fixture
def seeded_error_mep_elements():
    return [
        # Missing system type & missing dimensions
        {
            "id": 201,
            "category": "Ducts",
            "level": "Level 2",
            "parameters": {
                "System Type": "Undefined",
                "System Name": "",
            },
            "connectors": [],
        },
        # Open connector on duct fitting
        {
            "id": 202,
            "category": "Duct Fittings",
            "level": "Level 2",
            "parameters": {
                "System Type": "Supply Air",
                "System Name": "SA-02",
            },
            "connectors": [
                {"isConnected": True, "domain": "Hvac"},
                {"isConnected": False, "domain": "Hvac", "origin": {"x": 5.0, "y": 10.0, "z": 14.0}},
            ],
        },
        # Non-standard system prefix & zero flow
        {
            "id": 203,
            "category": "Ducts",
            "level": "Level 2",
            "parameters": {
                "System Type": "Supply Air",
                "System Name": "CustomDuctRun-99",
                "Width": 12.0,
                "Height": 8.0,
                "Flow": "0 CFM",
            },
            "connectors": [],
        },
        # Mechanical equipment with no clearance zone
        {
            "id": 204,
            "category": "Mechanical Equipment",
            "name": "Chiller CH-1",
            "level": "Basement",
            "parameters": {
                "System Type": "Chilled Water",
                "System Name": "CHW-01",
            },
            "connectors": [],
        },
    ]


def test_clean_model_has_zero_findings(clean_mep_elements):
    result = run_mep_audit(clean_mep_elements)
    assert result["total_elements_audited"] == 3
    assert result["total_findings"] == 0
    assert result["summary"]["errors"] == 0
    assert result["summary"]["warnings"] == 0


def test_seeded_errors_reported_accurately(seeded_error_mep_elements):
    result = run_mep_audit(seeded_error_mep_elements)
    assert result["total_elements_audited"] == 4
    assert result["total_findings"] > 0

    rule_ids = [f["ruleId"] for f in result["findings"]]
    # Should report SYS, CON, FLOW, and CLR rules
    assert "MEP-SYS-01" in rule_ids
    assert "MEP-CON-01" in rule_ids
    assert "MEP-FLOW-01" in rule_ids
    assert "MEP-CLR-01" in rule_ids

    # Unconnected connector error
    con_findings = [f for f in result["findings"] if f["ruleId"] == "MEP-CON-01"]
    assert len(con_findings) == 1
    assert con_findings[0]["elementId"] == 202
    assert con_findings[0]["severity"] == "error"

    # Missing clearance zone warning
    clr_findings = [f for f in result["findings"] if f["ruleId"] == "MEP-CLR-01"]
    assert len(clr_findings) == 1
    assert clr_findings[0]["elementId"] == 204


def test_cli_execution_with_output_file(tmp_path, seeded_error_mep_elements):
    in_file = tmp_path / "mep_in.json"
    out_file = tmp_path / "mep_out.json"

    with open(in_file, "w") as f:
        json.dump(seeded_error_mep_elements, f)

    script = os.path.join(HERE, "..", "scripts", "mep_qa.py")
    ret = os.system(f'python "{script}" "{in_file}" --output "{out_file}"')
    # Script exits with 1 when errors exist, but writes output file
    assert os.path.exists(out_file)

    with open(out_file, "r") as f:
        saved_data = json.load(f)
    assert saved_data["total_findings"] > 0
    assert "findings" in saved_data


@pytest.mark.parametrize("flow", [None, "", "unavailable", "NaN CFM", float("inf"), True])
def test_active_duct_reports_missing_or_unreadable_flow(clean_mep_elements, flow):
    duct = clean_mep_elements[0]
    duct["parameters"]["Flow"] = flow
    findings = run_mep_audit([duct])["findings"]
    assert [(finding["ruleId"], finding["severity"], finding["elementId"]) for finding in findings] == [
        ("MEP-FLOW-01", "warning", 101)
    ]
    assert "no readable calculated flow" in findings[0]["message"]


@pytest.mark.parametrize("flow", ["125 l/s", "1.4 m³/s", "40 Cfm", 0.5])
def test_positive_flow_is_clean(clean_mep_elements, flow):
    duct = clean_mep_elements[0]
    duct["parameters"]["Flow"] = flow
    assert run_mep_audit([duct])["findings"] == []


@pytest.mark.parametrize("flow", ["0 L/s", "-2 m³/h"])
def test_nonpositive_flow_keeps_its_diagnostic(clean_mep_elements, flow):
    duct = clean_mep_elements[0]
    duct["parameters"]["Flow"] = flow
    findings = run_mep_audit([duct])["findings"]
    assert [(finding["ruleId"], finding["elementId"]) for finding in findings] == [("MEP-FLOW-01", 101)]
    assert "zero or negative calculated flow" in findings[0]["message"]


@pytest.mark.parametrize("connectors", [None, [], {}, [{"domain": "Hvac"}], [{"isConnected": 0}],
                                        [{"isConnected": "false"}], [None]])
def test_missing_connector_evidence_is_not_a_verified_connection(clean_mep_elements, connectors):
    duct = clean_mep_elements[0]
    duct["connectors"] = connectors
    findings = run_mep_audit([duct])["findings"]
    assert [(row["ruleId"], row["severity"], row["elementId"]) for row in findings] == [
        ("MEP-CON-02", "warning", 101),
    ]


def test_partial_connector_evidence_preserves_verified_open_end(clean_mep_elements):
    duct = clean_mep_elements[0]
    duct["connectors"] = [{"isConnected": False, "origin": {"x": 1, "y": 2, "z": 3}}, {}]
    findings = run_mep_audit([duct])["findings"]
    assert [(row["ruleId"], row["severity"], row["elementId"]) for row in findings] == [
        ("MEP-CON-01", "error", 101),
        ("MEP-CON-02", "warning", 101),
    ]
    assert findings[0]["location"] == {"x": 1, "y": 2, "z": 3}
