import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "toposolid_grade_check.py"
compare = runpy.run_path(str(SCRIPT))["compare"]


def snapshot(triangles):
    return {"top_faces": [{"reference": "top", "triangles_ft": triangles}]}


def rectangle(x1, y1, x2, y2, height=lambda x, y: 0, reverse_diagonal=False):
    a, b, c, d = [(x, y, height(x, y)) for x, y in [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]]
    return [[a, b, d], [b, c, d]] if reverse_diagonal else [[a, b, c], [a, c, d]]


def square_with_hole(x1, x2):
    return snapshot(rectangle(0, 0, 10, 4) + rectangle(0, 6, 10, 10)
                    + rectangle(0, 4, x1, 6) + rectangle(x2, 4, 10, 6))


def test_same_sloped_grade_with_different_diagonal_and_orientation():
    slope = lambda x, y: 2 * x + 3 * y + 10
    first = rectangle(0, 0, 2, 4, slope)
    second = [list(reversed(row)) for row in rectangle(0, 0, 2, 4, slope, True)]
    assert compare(snapshot(first), snapshot(second), 0, 0) == {
        "matches_meshes": True, "source_uncovered_sqft": 0, "floor_extra_sqft": 0,
        "overlap_sqft": 8, "maximum_elevation_error_ft": 0,
    }


def test_same_corner_elevations_do_not_hide_changed_grade_break():
    a, b, c, d = (0, 0, 0), (2, 0, 0), (2, 2, 2), (0, 2, 0)
    first = snapshot([[a, b, c], [a, c, d]])
    second = snapshot([[a, b, d], [b, c, d]])
    report = compare(first, second, 0, 0.5)
    assert not report["matches_meshes"]
    assert report["maximum_elevation_error_ft"] == 1
    assert report["source_uncovered_sqft"] == report["floor_extra_sqft"] == 0


def test_equal_area_shifted_hole_fails_in_both_directions():
    report = compare(square_with_hole(3, 5), square_with_hole(4, 6), 0.01, 0)
    assert not report["matches_meshes"]
    assert report["source_uncovered_sqft"] == report["floor_extra_sqft"] == 2
    assert report["maximum_elevation_error_ft"] == 0


def test_missing_strip_and_disjoint_shapes_are_reported():
    source = snapshot(rectangle(0, 0, 10, 10))
    report = compare(source, snapshot(rectangle(0, 0, 9.9, 10)), 0.1, 0)
    assert not report["matches_meshes"]
    assert report["source_uncovered_sqft"] == pytest.approx(1)
    assert report["floor_extra_sqft"] == 0
    disjoint = compare(source, snapshot(rectangle(10, 0, 20, 10)), 1000, 0)
    assert not disjoint["matches_meshes"]
    assert disjoint["maximum_elevation_error_ft"] is None


@pytest.mark.parametrize(("bad_mesh", "message"), [
    (rectangle(0, 0, 2, 2) + rectangle(1, 1, 3, 3), "overlapping XY triangles"),
    (rectangle(0, 0, 2, 2) + rectangle(2, 0, 3, 2, lambda x, y: 1), "shared XY location"),
    ([[(0, 0, 0), (0, 0, 1), (2, 0, 2)]], "degenerate XY triangle"),
])
def test_rejects_invalid_surface_even_against_itself(bad_mesh, message):
    with pytest.raises(ValueError, match=message):
        compare(snapshot(bad_mesh), snapshot(bad_mesh), 0, 0)


def test_comparison_budget_stops_without_verdict(monkeypatch):
    monkeypatch.setitem(compare.__globals__, "MAX_PAIR_CHECKS", 1)
    mesh = snapshot(rectangle(0, 0, 2, 2))
    with pytest.raises(ValueError, match="1-pair budget"):
        compare(mesh, mesh, 0, 0)


def test_cli_consumes_query_record_and_distinguishes_pass_failure_and_stop(tmp_path):
    source = snapshot(rectangle(0, 0, 2, 2))
    floor = snapshot(rectangle(0, 0, 2, 2, reverse_diagonal=True))
    source_file, floor_file = tmp_path / "source.json", tmp_path / "floor.json"
    source_file.write_text(json.dumps({"result": {"status": "ok", "snapshot_json": json.dumps(source)}}))
    command = [sys.executable, str(SCRIPT), str(source_file), str(floor_file),
               "--area-tolerance-sqft", "0", "--elevation-tolerance-ft", "0"]
    floor_file.write_text(json.dumps(floor))
    passed = subprocess.run(command, capture_output=True, text=True)
    assert passed.returncode == 0 and json.loads(passed.stdout)["matches_meshes"]
    floor_file.write_text(json.dumps(snapshot(rectangle(0, 0, 3, 2))))
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode == 1 and json.loads(failed.stdout)["floor_extra_sqft"] == 2
    floor_file.write_text(json.dumps({"top_faces": []}))
    stopped = subprocess.run(command, capture_output=True, text=True)
    assert stopped.returncode == 2 and json.loads(stopped.stdout)["status"] == "stopped"
