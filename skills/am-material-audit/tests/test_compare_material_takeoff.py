import csv
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "skills/am-material-audit/scripts/compare_material_takeoff.py"
RENDER = ROOT / "skills/am-schedule-view/scripts/render-table.ps1"
HEADERS = ["material", "category", "level", "area_m2", "volume_m3"]


def quantity(material, area, volume, material_id=100):
    return {"material": material, "category": "Walls", "level": "Level 1",
            "material_id": material_id, "category_id": -2000011, "level_id": 10,
            "area_m2": area, "volume_m3": volume}


def query(path, rows, eid="9007199254740993", project="synthetic", issues=None):
    report = {"status": "reported" if not issues else "incomplete", "issues": issues or [],
              "project": {"uid": project, "path_sha256": "0" * 64},
              "rows": rows, "requested_elements": 1, "requested_element_ids": [eid]}
    path.write_text(json.dumps({"status": "succeeded", "result": {"payload": json.dumps(report)}}))
    return path


def invoke(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)


def compare(tmp_path, rows, *queries):
    schedule = tmp_path / "comparison.csv"
    with schedule.open("w", encoding="utf-8-sig", newline="") as stream:
        csv.writer(stream).writerows([HEADERS, *rows])
    args = [part for path in queries for part in ("--query", path)]
    return invoke("compare", "--schedule", schedule, *args)


def test_exact_tolerance_and_repeated_detail_rows(tmp_path):
    first = query(tmp_path / "first.json", [quantity("Café", "51", 0)])
    second = query(tmp_path / "second.json", [quantity("Café", "50", 0)], eid="2")
    detail = [["Café", "Walls", "Level 1", "45", "0"], ["Café", "Walls", "Level 1", "55", "0"]]
    result = compare(tmp_path, detail, first, second)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["queried_elements"] == 2
    assert report["groups"][0]["area_m2"] == {
        "query": "101", "schedule": "100", "difference_percent": "1.000000", "within_tolerance": True,
    }
    assert report["levels"][0]["volume_m3"] == {
        "query": "0", "schedule": "0", "difference_percent": None, "within_tolerance": True,
    }
    query(first, [quantity("Café", "51.00000001", 0)])
    result = compare(tmp_path, detail, first, second)
    assert result.returncode == 1
    assert json.loads(result.stdout)["groups"][0]["area_m2"]["within_tolerance"] is False


def test_matching_level_cannot_hide_group_errors_or_missing_groups(tmp_path):
    first = query(tmp_path / "first.json", [quantity("A", 11, 1), quantity("B", 9, 1, material_id=101)])
    result = compare(tmp_path, [["A", "Walls", "Level 1", 10, 1], ["B", "Walls", "Level 1", 10, 1]], first)
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert [row["status"] for row in report["groups"]] == ["difference", "difference"]
    assert report["levels"][0]["status"] == "match"

    result = compare(tmp_path, [["A", "Walls", "Level 1", 11, 0], ["C", "Walls", "Level 1", 9, 1]], first)
    assert result.returncode == 1
    assert [row["status"] for row in json.loads(result.stdout)["groups"]] == ["difference", "query_only", "schedule_only"]


def test_rejects_mixed_projects_overlap_collisions_and_partial_reads(tmp_path):
    first = query(tmp_path / "first.json", [quantity("A", 1, 1)])
    second = tmp_path / "second.json"
    detail = [["A", "Walls", "Level 1", 2, 2]]
    for report, diagnosis in [
        (query(second, [quantity("A", 1, 1)], "2", project="other"), "different saved projects"),
    ]:
        result = compare(tmp_path, detail, first, report)
        assert result.returncode == 2 and diagnosis in result.stderr
    result = compare(tmp_path, detail, first, query(second, [quantity("A", 1, 1)]))
    assert result.returncode == 2 and "overlap" in result.stderr
    result = compare(tmp_path, detail, first, query(second, [quantity(" A ", 1, 1, material_id=101)], "2"))
    assert result.returncode == 2 and "ambiguous material" in result.stderr
    failed = [{"element_id": 2, "reason": "quantity_read_failed"}]
    result = compare(tmp_path, detail, first, query(second, [], "2", issues=failed))
    assert result.returncode == 1
    assert json.loads(result.stdout)["issues"] == failed
    result = compare(tmp_path, [["A", "Walls", "Level 1", "-1", 1]], first)
    assert result.returncode == 2 and "out-of-range" in result.stderr
    result = compare(tmp_path, [["A", "Walls", "Level 1", "1e1000", 1]], first)
    assert result.returncode == 2 and "out-of-range" in result.stderr


@pytest.mark.parametrize("runtime", [
    {"mode": "dry_run"},
    {"result_truncated": True},
    {"exit_code": 1},
    {"changes": {"added_count": 0, "modified_count": 1, "deleted_count": 0}},
    {"changes": {"added_count": 0, "modified_count": 0, "deleted_count": 0, "deleted": ["7"]}},
])
def test_query_runtime_must_be_complete_and_read_only(tmp_path, runtime):
    takeoff = query(tmp_path / "query.json", [quantity("A", 3, 1)])
    record = json.loads(takeoff.read_text())
    record.update(runtime)
    takeoff.write_text(json.dumps(record))
    result = compare(tmp_path, [["A", "Walls", "Level 1", 3, 1]], takeoff)
    assert result.returncode == 1
    assert json.loads(result.stdout)["status"] == "incomplete"


def test_complete_get_run_response_can_be_used_for_comparison(tmp_path):
    takeoff = query(tmp_path / "query.json", [quantity("A", 3, 1)])
    record = json.loads(takeoff.read_text())
    record.update(mode="query", status="ok", result_truncated=False,
                  changes={"added_count": 0, "modified_count": 0, "deleted_count": 0})
    takeoff.write_text(json.dumps({"response": record}))
    result = compare(tmp_path, [["A", "Walls", "Level 1", 3, 1]], takeoff)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "match"


def test_schedule_metadata_and_existing_csv_export(tmp_path):
    powershell = shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell is required for the existing CSV exporter")
    schedule = {"name": "Sample takeoff", "id": 12, "category": "Walls",
                "kind": "material takeoff", "itemized": True, "truncated": False, "totalRows": 2,
                "columns": HEADERS, "rows": [["Café", "Walls", "Level 1", "3", "1"], ["", "", "", "", ""]],
                "rowKinds": ["body", "blank"]}
    record = tmp_path / "record.json"

    def write_record():
        record.write_text(json.dumps({"status": "succeeded", "result": {"payload": json.dumps({
            "kind": "schedules", "missing": [], "schedules": [schedule],
        })}}))

    write_record()
    result = invoke("validate-schedule", record, "12")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["detail_rows"] == 1
    exports = tmp_path / "exports"
    subprocess.run([powershell, "-NoProfile", "-File", RENDER, "-Json", record, "-Out",
                    tmp_path / "preview.html", "-CsvDir", exports], check=True, capture_output=True)
    extracted = exports / "Sample takeoff.csv"
    takeoff = query(tmp_path / "query.json", [quantity("Café", 3, 1)])
    assert invoke("compare", "--query", takeoff, "--schedule", extracted).returncode == 0
    schedule["truncated"] = True
    write_record()
    assert invoke("validate-schedule", record, "12").returncode == 1
    schedule["truncated"] = False
    schedule["rowKinds"][1] = "total"
    write_record()
    assert invoke("validate-schedule", record, "12").returncode == 1
    schedule["kind"] = "schedule"
    write_record()
    assert invoke("validate-schedule", record, "12").returncode == 2
