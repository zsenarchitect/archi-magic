import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
profiles_match = runpy.run_path(str(SCRIPTS / "toposolid_profile_check.py"))["profiles_match"]


def line(start, end):
    return {"kind": "line", "points_ft": [list(start), list(end)]}


def rectangle(x1, y1, x2, y2, z=0):
    points = [(x1, y1, z), (x2, y1, z), (x2, y2, z), (x1, y2, z)]
    return [line(start, end) for start, end in zip(points, points[1:] + points[:1])]


def semicircle(shift=0, bulge=1, z=0):
    start, end = (-1 + shift, 0, z), (1 + shift, 0, z)
    return [{"kind": "arc", "points_ft": [list(start), [shift, bulge, z], list(end)],
             "center_ft": [shift, 0, z]}, line(end, start)]


def test_matches_rotated_reversed_curves_and_reordered_holes():
    outer = rectangle(-5, -5, 5, 5)
    inner = semicircle()
    floor_outer = rectangle(-5, -5, 5, 5, z=23)
    floor_inner = semicircle(z=23)
    for item in floor_inner:
        item["points_ft"].reverse()
    assert profiles_match([outer, inner], [floor_inner[::-1], floor_outer[2:] + floor_outer[:2]], 0)


def test_bounds_the_entire_arc_and_rejects_complementary_sweep():
    assert profiles_match([semicircle()], [semicircle(shift=0.005)], 0.01)
    assert not profiles_match([semicircle()], [semicircle(shift=0.05)], 0.01)
    assert not profiles_match([semicircle()], [semicircle(bulge=-1)], 0.01)


def test_rejects_shifted_hole_or_arc_replaced_by_chord():
    outer = rectangle(-5, -5, 5, 5)
    assert not profiles_match([outer, rectangle(0, 0, 1, 1)], [outer, rectangle(1, 0, 2, 1)], 0.01)
    triangle = [line((-1, 0, 0), (0, 1, 0)), line((0, 1, 0), (1, 0, 0)), line((1, 0, 0), (-1, 0, 0))]
    assert not profiles_match([semicircle()], [triangle], 0.01)
    malformed = semicircle()
    malformed[0]["center_ft"][1] = 5
    with pytest.raises(ValueError, match="native center or sweep"):
        profiles_match([malformed], [semicircle()], 0.01)


def test_floor_cli_requires_mesh_profile_identity_and_actual_offset(tmp_path):
    common = {
        "document": {"uid": "project", "path_sha256": "project-path"},
        "floor_type": {"id": "3", "uid": "floor-type", "version": "1"},
        "level": {"id": "4", "uid": "level", "version": "2"},
        "level_project_elevation_ft": 10,
        "profile_loops": [rectangle(0, 0, 1, 1)],
        "top_faces": [{"reference": "face", "triangles_ft": [
            [[0, 0, 10], [1, 0, 10], [1, 1, 10]], [[0, 0, 10], [1, 1, 10], [0, 1, 10]],
        ]}],
    }
    source, floor = dict(common, region={"uid": "terrain"}), dict(common, floor={"uid": "floor"}, floor_offset_ft=2)
    first, second = tmp_path / "source.json", tmp_path / "floor.json"
    first.write_text(json.dumps(source))
    command = [sys.executable, str(SCRIPTS / "toposolid_grade_check.py"), str(first), str(second),
               "--area-tolerance-sqft", "0", "--elevation-tolerance-ft", "0.01",
               "--xy-tolerance-ft", "0.01", "--expected-floor-offset-ft", "2"]

    def compare_candidate(candidate, expected_code):
        second.write_text(json.dumps({"status": "ok", "snapshot_json": json.dumps(candidate)}))
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode == expected_code, result.stderr + result.stdout
        return json.loads(result.stdout)

    assert compare_candidate(floor, 0)["matches_floor"]
    candidate = dict(floor, profile_loops=[rectangle(0.1, 0, 1.1, 1)])
    result = compare_candidate(candidate, 1)
    assert result["matches_meshes"] and not result["matches_profile"]
    assert compare_candidate(dict(floor, floor_offset_ft=2.1), 1)["floor_offset_error_ft"] == pytest.approx(0.1)
    assert not compare_candidate(dict(floor, level_project_elevation_ft=20), 1)["matches_level"]
    assert not compare_candidate(dict(floor, floor_type=dict(common["floor_type"], version="3")), 1)["matches_floor_type"]
    assert not compare_candidate(dict(floor, document=dict(common["document"], uid="other")), 1)["matches_document"]
