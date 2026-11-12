from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mep_sizing_preview.py"


def entry(identifier, service, profile, system, demand_kind, demand_unit):
    return {"id": identifier, "service": service, "profile": profile, "system": system,
            "demand_kind": demand_kind, "demand_unit": demand_unit}


def run_preview(tmp_path, data):
    source = tmp_path / "sizing.json"
    raw = json.dumps(data).encode()
    source.write_bytes(raw)
    result = subprocess.run([sys.executable, str(SCRIPT), str(source)], capture_output=True, text=True)
    return result, sha256(raw).hexdigest()


def test_picks_first_approved_rule_with_enough_capacity_for_all_services(tmp_path):
    duct = entry("duct", "duct", "oval", "Supply Air", "flow", "L/s")
    pipe = entry("pipe", "pipe", "round", "Cold Water", "flow", "gpm")
    conduit = entry("conduit", "conduit", "round", "Lighting", "fill", "mm2")
    tray = entry("tray", "cable_tray", "rectangular", "Data", "fill", "in2")
    rules = [
        dict(duct, id="D-100", max_demand=100.01, dimensions=[300, 200], dimension_unit="mm"),
        dict(duct, id="D-200", max_demand=200, dimensions=[450, 250], dimension_unit="mm"),
        dict(pipe, id="P-12", max_demand=12, dimensions=[1.25], dimension_unit="in"),
        dict(conduit, id="C-40", max_demand=40, dimensions=[25], dimension_unit="mm"),
        dict(tray, id="T-7", max_demand=7, dimensions=[6, 2], dimension_unit="in"),
    ]
    requests = [
        dict(duct, id="duct-at-capacity", demand=100.01),
        dict(duct, id="duct-over-capacity", demand=100.01001),
        dict(pipe, demand=5),
        dict(conduit, demand=40),
        dict(tray, demand=7),
    ]
    result, digest = run_preview(tmp_path, {"table_id": "Fixture table", "rules": rules, "requests": requests})
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert preview["basis_sha256"] == digest
    assert [row[-2:] for row in preview["after"]] == [
        ["300 × 200 mm", "D-100"], ["450 × 250 mm", "D-200"], ["1.25 in", "P-12"],
        ["25 mm", "C-40"], ["6 × 2 in", "T-7"],
    ]
    assert preview["review_only"] and not preview["blocked"]
    assert preview["steps"] == preview["ownership_ids"] == preview["warnings"] == []
    saved, rendered = tmp_path / "preview.json", tmp_path / "review.html"
    saved.write_text(result.stdout)
    project_page = SCRIPT.parents[2] / "am-bim-viewer" / "scripts" / "project_page.py"
    page = subprocess.run([sys.executable, str(project_page), "--title", "Sizing fixture",
                           "--preview", str(saved), "--out", str(rendered)], capture_output=True, text=True)
    assert page.returncode == 0, page.stderr
    assert "450 × 250 mm" in rendered.read_text()


@pytest.mark.parametrize("difference", ["unit", "profile", "system", "demand_type", "capacity"])
def test_unmatched_or_over_capacity_requests_have_blocking_preview_rows(tmp_path, difference):
    row = entry("duct-1", "duct", "round", "Supply Air", "flow", "L/s")
    rule = dict(row, id="D-20", max_demand=20, dimensions=[150], dimension_unit="mm")
    request = dict(row, demand=10)
    field, value = {
        "unit": ("demand_unit", "cfm"),
        "profile": ("profile", "rectangular"),
        "system": ("system", "Return Air"),
        "demand_type": ("demand_kind", "velocity"),
        "capacity": ("demand", 21),
    }[difference]
    request[field] = value
    result, _ = run_preview(tmp_path, {"table_id": "Fixture table", "rules": [rule], "requests": [request]})
    assert result.returncode == 1, result.stderr
    preview = json.loads(result.stdout)
    assert preview["after"][0][-2:] == [None, None]
    assert preview["blocked"] and preview["review_only"] and preview["steps"] == []
    warning, = preview["warnings"]
    assert warning["blocking"] and warning["row"] == "duct-1"
    assert ("exceeds" in warning["text"]) is (difference == "capacity")


@pytest.mark.parametrize("invalid", ["negative", "nonfinite", "dimensions", "repeated_rule"])
def test_invalid_sizing_inputs_emit_no_preview(tmp_path, invalid):
    row = entry("pipe-1", "pipe", "round", "Cold Water", "flow", "gpm")
    rule = dict(row, id="P-20", max_demand=20, dimensions=[1], dimension_unit="in")
    data = {"table_id": "Fixture table", "rules": [rule], "requests": [dict(row, demand=5)]}
    if invalid == "negative":
        data["requests"][0]["demand"] = -1
    elif invalid == "nonfinite":
        rule["max_demand"] = float("inf")
    elif invalid == "dimensions":
        rule["dimensions"] = [1, 2]
    else:
        data["rules"].append(dict(rule))
    result, _ = run_preview(tmp_path, data)
    assert result.returncode == 2 and result.stdout == ""
