from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mep_route_preview.py"


def fixture():
    return {
        "endpoints": {
            "kind": "mep-route-endpoints", "version": 1,
            "source": {"host_identity": {"project_uid": "fixture"}},
            "endpoints": [
                {"element_id": "41", "element_uid": "first", "connector_id": 1,
                 "origin_ft": [0, 0, 0], "axes": [[0, 1, 0], [0, 0, 1], [1, 0, 0]],
                 "domain": "DomainHvac", "shape": "Round", "dimensions_ft": [1]},
                {"element_id": "42", "element_uid": "last", "connector_id": 2,
                 "origin_ft": [10, 0, 0], "axes": [[0, 1, 0], [0, 0, -1], [-1, 0, 0]],
                 "domain": "DomainHvac", "shape": "Round", "dimensions_ft": [1]},
            ],
        },
        "minimum_segment_ft": 0.25, "clearance_ft": 0.25, "obstacles": [],
    }


def run(tmp_path, data):
    source = tmp_path / "route-input.json"
    source.write_text(json.dumps(data))
    return subprocess.run([sys.executable, str(SCRIPT), str(source)], capture_output=True, text=True)


def test_straight_route_respects_open_end_axes_and_renders(tmp_path):
    result = run(tmp_path, fixture())
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    route = preview["route"]
    assert route["points_ft"] == [[0, 0, 0], [10, 0, 0]]
    assert route["length_ft"] == 10 and route["bends_degrees"] == []
    assert preview["review_only"] and preview["steps"] == preview["ownership_ids"] == []
    saved, rendered = tmp_path / "preview.json", tmp_path / "review.html"
    saved.write_text(result.stdout)
    project_page = SCRIPT.parents[2] / "am-bim-viewer" / "scripts" / "project_page.py"
    view = subprocess.run([sys.executable, str(project_page), "--title", "Route fixture",
                           "--preview", str(saved), "--out", str(rendered)], capture_output=True, text=True)
    assert view.returncode == 0, view.stderr
    assert "Waypoints (host ft)" in rendered.read_text()


def test_detour_preserves_clearance_and_checks_diagonal_segments(tmp_path):
    data = fixture()
    data["obstacles"] = [{"min_ft": [4, -1, -1], "max_ft": [6, 1, 1]}]
    result = run(tmp_path, data)
    assert result.returncode == 0, result.stderr
    route = json.loads(result.stdout)["route"]
    assert route["length_ft"] > 10 and 45 in route["bends_degrees"]
    assert set(route["bends_degrees"]) <= {45, 90}
    # The swept segment crossing the middle of the obstruction must clear
    # its one-foot half-width, half-foot connector radius and quarter-foot gap.
    for a, b in zip(route["points_ft"], route["points_ft"][1:]):
        if min(a[0], b[0]) < 5 < max(a[0], b[0]):
            fraction = (5 - a[0]) / (b[0] - a[0])
            y, z = (a[i] + fraction * (b[i] - a[i]) for i in (1, 2))
            assert max(abs(y), abs(z)) > 1.75
            break
    else:
        pytest.fail("No segment crosses the obstruction plane.")
    assert run(tmp_path, data).stdout == result.stdout


def test_obstacle_covering_endpoint_blocks_route_without_edit_steps(tmp_path):
    data = fixture()
    data["obstacles"] = [{"min_ft": [-0.2, -0.2, -0.2], "max_ft": [0.2, 0.2, 0.2]}]
    result = run(tmp_path, data)
    assert result.returncode == 1, result.stderr
    preview = json.loads(result.stdout)
    assert preview["route"] is None and preview["blocked"]
    assert preview["steps"] == [] and preview["warnings"][0]["blocking"]


@pytest.mark.parametrize("change", ["domain", "diameter", "normal", "bounds"])
def test_malformed_or_incompatible_route_input_emits_no_preview(tmp_path, change):
    data = deepcopy(fixture())
    last = data["endpoints"]["endpoints"][1]
    if change == "domain":
        last["domain"] = "DomainPiping"
    elif change == "diameter":
        last["dimensions_ft"] = [2]
    elif change == "normal":
        last["axes"][2] = [0, 0, 0]
    else:
        data["obstacles"] = [{"min_ft": [3, 0, 0], "max_ft": [2, 1, 1]}]
    result = run(tmp_path, data)
    assert result.returncode == 2 and not result.stdout


def test_alternating_obstacles_require_multiple_detours_inside_routing_area(tmp_path):
    data = fixture()
    data["routing_bounds_ft"] = {"min_ft": [-1, -3, -1], "max_ft": [11, 3, 1]}
    data["obstacles"] = [
        {"min_ft": [2, -3, -1], "max_ft": [3, 0, 1]},
        {"min_ft": [6, 0, -1], "max_ft": [7, 3, 1]},
    ]
    result = run(tmp_path, data)
    assert result.returncode == 0, result.stderr
    route = json.loads(result.stdout)["route"]
    points = route["points_ft"]
    assert max(point[1] for point in points) > 0.75
    assert min(point[1] for point in points) < -0.75
    assert all(-2.25 <= y <= 2.25 and -0.25 <= z <= 0.25 for _, y, z in points)
    assert points[0] == [0, 0, 0] and points[-1] == [10, 0, 0]
    assert set(route["bends_degrees"]) <= {45, 90}
    for station, sign in ((2.5, 1), (6.5, -1)):
        crossings = []
        for a, b in zip(points, points[1:]):
            if min(a[0], b[0]) < station < max(a[0], b[0]):
                fraction = (station - a[0]) / (b[0] - a[0])
                crossings.append(a[1] + fraction * (b[1] - a[1]))
        assert crossings and all(sign * y > 0.75 for y in crossings)

    data["obstacles"][0]["max_ft"][1] = 3
    blocked = run(tmp_path, data)
    assert blocked.returncode == 1, blocked.stderr
    assert json.loads(blocked.stdout)["route"] is None


def test_native_obstacle_record_is_used_and_bound_to_the_endpoint_host(tmp_path):
    data = fixture()
    del data["obstacles"]
    record = {
        "kind": "mep-route-obstacles", "version": 1,
        "source": {"host_identity": data["endpoints"]["source"]["host_identity"], "link_instance_id": "12"},
        "obstacles": [{"element_id": "100", "element_uid": "wall", "min_ft": [4, -1, -1], "max_ft": [6, 1, 1]}],
    }
    data["obstacle_records"] = [record]
    result = run(tmp_path, data)
    assert result.returncode == 0, result.stderr
    route = json.loads(result.stdout)["route"]
    assert route["length_ft"] > 10 and route["obstacle_records"] == [record]
    record["source"]["host_identity"] = {"project_uid": "other"}
    mismatch = run(tmp_path, data)
    assert mismatch.returncode == 2 and "same host" in mismatch.stderr
    assert mismatch.stdout == ""


def test_elbow_evidence_blocks_bent_routes_and_keeps_straight_candidates(tmp_path):
    data = fixture()
    record = {
        "kind": "mep-route-elbow", "version": 1, "source": data["endpoints"]["source"],
        "fitting": {"curve_type_id": "42", "curve_type_uid": "type", "domain": "DomainHvac",
                    "diameter_ft": 1, "elbow": None},
    }
    data["elbow_record"] = record
    straight = run(tmp_path, data)
    assert straight.returncode == 0
    assert json.loads(straight.stdout)["route"]["bends_degrees"] == []
    data["obstacles"] = [{"min_ft": [4, -1, -1], "max_ft": [6, 1, 1]}]
    blocked = run(tmp_path, data)
    assert blocked.returncode == 1
    assert "no matching elbow" in json.loads(blocked.stdout)["warnings"][0]["text"]
    record["fitting"]["elbow"] = {"id": "43", "uid": "elbow"}
    detour = run(tmp_path, data)
    assert detour.returncode == 0, detour.stderr
    assert json.loads(detour.stdout)["route"]["elbow_record"] == record
    record["fitting"]["diameter_ft"] = 2
    invalid = run(tmp_path, data)
    assert invalid.returncode == 2 and "domain and diameter" in invalid.stderr
