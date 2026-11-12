from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from mep_penetration_preview import preview


@pytest.fixture
def evidence():
    return {
        "kind": "mep-penetration-evidence",
        "frame": "revit_host_internal_feet",
        "host_identity": {"project_uid": "host-model", "path_sha256": "a" * 64},
        "link_identity": {"project_uid": "link-model", "path_sha256": "b" * 64},
        "link_instance_uid": "linked-instance",
        "clearance_ft": 0.25,
        "tolerance_ft": 0.001,
        "complete": True,
        "hosts": [{"uid": "wall-10", "id": "10", "kind": "wall", "gross_sha256": "c" * 64,
                   "depth_ft": 1.0, "normal": [1, 0, 0], "cuts_complete": True, "existing": [],
                   "minimum_web_ft": 0.5}],
        "runs": [{"uid": "duct-20", "id": "20", "system": "Supply Air",
                  "profile": {"shape": "rectangular", "width": 2.0, "height": 1.0}}],
        "pairs": [{"host_uid": "wall-10", "run_uid": "duct-20", "status": "solid_crossing",
                   "entry_ft": [0, 2, 3], "exit_ft": [1, 2, 3], "center_uv_ft": [2, 3], "angle_rad": 0,
                   "edge_distance_ft": 3.0, "gross_verified": True}],
        "tracked": [],
    }


def track(evidence):
    row, = preview(evidence)["findings"]
    assert row["status"] == "proposed"
    evidence["tracked"] = [{"host_uid": row["host_uid"], "run_uid": row["run_uid"],
                            "crossing_key": row["crossing_key"], "opening_uid": "opening-30",
                            "observed_uid": "opening-30", "observed_host_uid": "wall-10",
                            "cut_verified": True, "geometry": row["geometry"],
                            "geometry_sha256": row["geometry_sha256"], "opening_sha256": "d" * 64,
                            "observed_opening_sha256": "d" * 64}]
    return row["crossing_key"]


@pytest.mark.parametrize("shape, dimension, size", [("rectangular", "width", 2.5), ("round", "diameter", 2.0)])
def test_cli_sizes_reviewed_crossing_with_explicit_clearance(tmp_path, evidence, shape, dimension, size):
    if shape == "round":
        evidence["runs"][0]["profile"] = {"shape": shape, "diameter": 1.5}
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence))
    run = subprocess.run([sys.executable, str(SCRIPTS / "mep_penetration_preview.py"), str(path)],
                         capture_output=True, text=True, check=True)
    row, = json.loads(run.stdout)["findings"]
    assert row["ruleId"] == "MEP-PEN-01"
    assert row["status"] == "proposed"
    assert row["geometry"]["outline"][dimension] == size
    assert row["location"] == [0.5, 2.0, 3.0]
    assert row["system"] == "Supply Air"


@pytest.mark.parametrize("change, status", [
    ("actual_void", "current"),
    ("small_measurement_noise", "current"),
    ("moved_within_host", "out_of_date"),
    ("moved_away", "out_of_date"),
    ("changed_opening", "out_of_date"),
    ("missing_link", "unresolved"),
    ("partial", "unresolved"),
    ("unverified_miss", "unresolved"),
    ("copied_tag", "unresolved"),
    ("lost_cut", "unresolved"),
    ("duplicate", "duplicate"),
])
def test_tracking_distinguishes_motion_missing_evidence_and_existing_voids(evidence, change, status):
    key = track(evidence)
    pair = evidence["pairs"][0]
    pair.update(status="gross_crossing", existing_cut_verified=True)
    if change in {"small_measurement_noise", "moved_within_host"}:
        shift = 0.0001 if change == "small_measurement_noise" else 1.0
        pair["entry_ft"][1] += shift
        pair["exit_ft"][1] += shift
        pair["center_uv_ft"][0] += shift
    elif change in {"moved_away", "unverified_miss"}:
        evidence["pairs"][0] = dict(pair, status="no_crossing", gross_verified=change == "moved_away")
    elif change == "changed_opening":
        evidence["tracked"][0]["observed_opening_sha256"] = "e" * 64
    elif change == "missing_link":
        evidence["runs"].clear()
        evidence["pairs"].clear()
        evidence["complete"] = False
    elif change == "partial":
        evidence["complete"] = False
    elif change == "copied_tag":
        evidence["tracked"][0]["observed_uid"] = "different-opening"
    elif change == "lost_cut":
        evidence["tracked"][0]["cut_verified"] = False
    elif change == "duplicate":
        evidence["tracked"].append(dict(evidence["tracked"][0]))
    row, = preview(evidence)["findings"]
    assert row["crossing_key"] == key
    assert row["status"] == status


@pytest.mark.parametrize("change", ["angled", "shallow", "near_edge", "unknown_cut", "missing_pair"])
def test_incomplete_or_unsupported_geometry_does_not_generate_an_opening(evidence, change):
    pair = evidence["pairs"][0]
    if change == "angled":
        pair["exit_ft"][1] += 0.5
    elif change == "shallow":
        pair["exit_ft"][0] -= 0.5
    elif change == "near_edge":
        pair["edge_distance_ft"] = 0.5
    elif change == "unknown_cut":
        evidence["hosts"][0]["cuts_complete"] = False
    elif change == "missing_pair":
        evidence["runs"].append(dict(evidence["runs"][0], uid="duct-21", id="21"))
    row, = preview(evidence)["findings"]
    assert row["status"] == "unresolved"


def test_spacing_blocks_both_proposals_on_same_host(evidence):
    evidence["runs"].append(dict(evidence["runs"][0], uid="duct-21", id="21"))
    pair = deepcopy(evidence["pairs"][0])
    pair.update(run_uid="duct-21", entry_ft=[0, 4.6, 3], exit_ft=[1, 4.6, 3], center_uv_ft=[4.6, 3])
    evidence["pairs"].append(pair)
    rows = preview(evidence)["findings"]
    assert len(rows) == 2
    assert all(row["status"] == "unresolved" for row in rows)
    assert rows[0]["spacing_conflicts"][0]["gap_ft"] == pytest.approx(0.1)


def test_vertical_round_run_can_cross_a_floor(evidence):
    host = evidence["hosts"][0]
    host.update(kind="floor", normal=[0, 0, 1], depth_ft=0.5)
    pair = evidence["pairs"][0]
    pair.update(entry_ft=[2, 3, 8], exit_ft=[2, 3, 8.5])
    evidence["runs"][0]["profile"] = {"shape": "round", "diameter": 0.25}
    row, = preview(evidence)["findings"]
    assert row["status"] == "proposed"
    assert row["geometry"]["outline"]["diameter"] == 0.75
    assert row["location"] == [2, 3, 8.25]


def test_second_placement_of_same_link_cannot_inherit_a_tracking_record(evidence):
    old_key = track(evidence)
    evidence["link_instance_uid"] = "another-link-instance"
    evidence["pairs"][0]["status"] = "gross_crossing"
    row, = preview(evidence)["findings"]
    assert row["crossing_key"] != old_key
    assert row["status"] == "unresolved"
