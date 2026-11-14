import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def derive(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from toposolid_floor_shape import derive
    return derive


def profile(points):
    return [{"kind": "line", "points_ft": [a, b]} for a, b in zip(points, points[1:] + points[:1])]


def snapshot(mesh, loops=None):
    if loops is None:
        loops = [profile([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]])]
    return {"top_faces": [{"triangles_ft": mesh}], "profile_loops": loops, "level_project_elevation_ft": 10}


def square(height):
    return [[x, y, height(x, y)] for x, y in [(0, 0), (2, 0), (2, 2), (0, 2)]]


def test_flat_and_sloped_planes_omit_tessellation_diagonals(derive):
    a, b, c, d = square(lambda x, y: 11)
    result = derive(snapshot([[a, b, c], [a, c, d]]), 1)
    assert result == {"boundary_ft": [], "interior_ft": [], "split_lines_xy_ft": []}
    a, b, c, d = square(lambda x, y: 10 + x)
    result = derive(snapshot([[a, b, c], [a, c, d]]), 0)
    assert result == {"boundary_ft": [b, c], "interior_ft": [], "split_lines_xy_ft": []}


def test_interior_peak_gets_only_actual_grade_breaks(derive):
    corners = square(lambda x, y: 10)
    peak = [1, 1, 12]
    mesh = [[a, b, peak] for a, b in zip(corners, corners[1:] + corners[:1])]
    result = derive(snapshot(mesh), 0)
    assert result["boundary_ft"] == []
    assert result["interior_ft"] == [peak]
    assert result["split_lines_xy_ft"] == [
        [[0, 0], [1, 1]], [[0, 2], [1, 1]], [[1, 1], [2, 0]], [[1, 1], [2, 2]],
    ]


def test_flat_curved_boundary_and_hole_have_no_shape_edits(derive):
    outer = profile([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]])
    outer[0] = {"kind": "arc", "points_ft": [[0, 0, 0], [1, -1, 0], [2, 0, 0]], "center_ft": [1, 0, 0]}
    a, b, c, d = square(lambda x, y: 10)
    mesh = [[a, b, c], [a, c, d], [a, [1, -1, 10], b]]
    assert derive(snapshot(mesh, [outer]), 0)["interior_ft"] == []

    mesh = []
    for x in range(3):
        for y in range(3):
            if (x, y) == (1, 1):
                continue
            a, b, c, d = [[i, j, 10] for i, j in [(x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1)]]
            mesh.extend([[a, b, c], [a, c, d]])
    outer = profile([[0, 0, 0], [3, 0, 0], [3, 3, 0], [0, 3, 0]])
    hole = profile([[1, 1, 0], [1, 2, 0], [2, 2, 0], [2, 1, 0]])
    assert derive(snapshot(mesh, [outer, hole]), 0) == {
        "boundary_ft": [], "interior_ft": [], "split_lines_xy_ft": [],
    }


def test_stops_if_boundary_needs_a_new_shape_vertex(derive):
    a, b, c, d = square(lambda x, y: 10)
    midpoint = [1, 0, 11]
    mesh = [[a, midpoint, d], [midpoint, b, c], [midpoint, c, d]]
    with pytest.raises(ValueError, match="unsupported vertex"):
        derive(snapshot(mesh), 0)


def test_stops_for_missing_region_or_nonconforming_mesh(derive):
    a, b, c, d = square(lambda x, y: 10)
    with pytest.raises(ValueError, match="native sketch|boundary mesh vertex"):
        derive(snapshot([[a, b, c]]), 0)
    midpoint = [1, 1, 10]
    with pytest.raises(ValueError, match="native sketch"):
        derive(snapshot([[a, b, c], [a, midpoint, d], [midpoint, c, d]]), 0)


def test_cli_emits_probe_shape_from_saved_query_record(tmp_path):
    a, b, c, d = square(lambda x, y: 10)
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"result": {"status": "ok", "snapshot_json": json.dumps(
        snapshot([[a, b, c], [a, c, d]]))}}))
    result = subprocess.run([sys.executable, str(SCRIPTS / "toposolid_floor_shape.py"), str(source),
                             "--floor-offset-ft", "0"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"boundary_ft": [], "interior_ft": [], "split_lines_xy_ft": []}
