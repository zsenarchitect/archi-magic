from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "element_split_preview.py"


def element(category, number, geometry, cuts):
    return {"id": str(number), "uid": f"element-{number}", "type_id": "500", "type_uid": "type-500",
            "category": category, "geometry": geometry, "cuts": cuts}


def level(identifier, elevation):
    return {"id": str(identifier), "uid": f"level-{identifier}", "kind": "level", "elevation_ft": elevation}


def line(identifier, kind, start, end):
    return {"id": str(identifier), "uid": f"{kind}-{identifier}", "kind": kind,
            "start_xy_ft": start, "end_xy_ft": end}


def floor(cuts, boundary=None):
    return element("floor", 40, {"elevation_ft": -1.5,
                   "boundary_xy_ft": boundary or [[0, 0], [8, 0], [8, 4], [0, 4]]}, cuts)


def run(tmp_path, sources, minimum=0.01):
    snapshot = {"kind": "element-split-evidence", "frame": "revit_host_internal_feet",
                "host_identity": {"project_uid": "fixture", "path_sha256": "a" * 64},
                "minimum_piece_ft": minimum, "elements": sources}
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
    return result, sha256(path.read_bytes()).hexdigest()


def test_multiple_level_splits_and_grid_plus_finite_floor_cut_render(tmp_path):
    categories = ("wall", "architectural_column", "structural_column")
    sources = [element(category, i, {"base_ft": -2, "top_ft": 26}, [level(51, 17), level(50, 8)])
               for i, category in enumerate(categories, 30)]
    sources.append(floor([line(60, "grid", [4, 1], [4, 3]), line(61, "line", [0, 2], [4, 2])]))
    result, digest = run(tmp_path, sources)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["basis_sha256"] == digest
    assert report["review_only"] is True and report["steps"] == report["ownership_ids"] == []
    for category, item in zip(categories, report["findings"]):
        assert item["category"] == category and item["type_id"] == "500"
        assert item["pieces"] == [{"base_ft": -2, "top_ft": 8}, {"base_ft": 8, "top_ft": 17},
                                  {"base_ft": 17, "top_ft": 26}]
    split = report["findings"][-1]
    assert split["seams"] == [{"cut_id": "60", "segments_xy_ft": [[[4, 0], [4, 4]]]},
                              {"cut_id": "61", "segments_xy_ft": [[[0, 2], [4, 2]]]}]
    expected_vertices = [
        {(0, 2), (4, 2), (4, 4), (0, 4)},
        {(0, 0), (4, 0), (4, 2), (0, 2)},
        {(4, 0), (8, 0), (8, 4), (4, 4)},
    ]
    assert [{tuple(vertex) for vertex in part["boundary_xy_ft"]} for part in split["pieces"]] == expected_vertices
    assert [part["area_sqft"] for part in split["pieces"]] == [8, 8, 16]
    assert all(part["elevation_ft"] == -1.5 for part in split["pieces"])
    saved, out = tmp_path / "preview.json", tmp_path / "project.html"
    saved.write_text(result.stdout)
    render = SCRIPT.parents[2] / "am-bim-viewer/scripts/project_page.py"
    view = subprocess.run([sys.executable, str(render), "--preview", str(saved), "--out", str(out)],
                          capture_output=True, text=True)
    assert view.returncode == 0, view.stderr
    assert "Candidate element splits" in out.read_text()


@pytest.mark.parametrize("change,warning", [
    ("endpoint", "distinct interior"),
    ("duplicate_elevation", "distinct interior"),
    ("tiny_piece", "undersized"),
    ("short_line", "span the full floor seam"),
    ("edge_only", "does not cross"),
    ("crossed", "simple convex"),
    ("concave", "simple convex"),
])
def test_blocked_geometry_never_yields_pieces(tmp_path, change, warning):
    if change in ("endpoint", "duplicate_elevation", "tiny_piece"):
        heights = {"endpoint": [0], "duplicate_elevation": [8, 8], "tiny_piece": [0.005]}[change]
        source = element("wall", 30, {"base_ft": 0, "top_ft": 20},
                         [level(i, height) for i, height in enumerate(heights, 50)])
    elif change == "crossed":
        source = floor([line(60, "grid", [4, 1], [4, 3])], [[0, 0], [8, 4], [8, 0], [0, 4]])
    elif change == "concave":
        source = floor([line(60, "grid", [4, 1], [4, 3])], [[0, 0], [8, 0], [2, 2], [8, 4], [0, 4]])
    else:
        start, end = ([4, 1], [4, 3]) if change == "short_line" else ([0, 0], [8, 0])
        source = floor([line(60, "line", start, end)])
    result, _ = run(tmp_path, [source])
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["blocked"] and report["review_only"]
    assert report["findings"] == report["steps"] == []
    assert warning in report["warnings"][0]["text"]


def test_batch_expansion_limit_returns_no_partial_preview(tmp_path):
    cuts = [level(i + 50, i + 1) for i in range(10)]
    sources = [element("wall", i, {"base_ft": 0, "top_ft": 11}, cuts) for i in range(30, 40)]
    result, _ = run(tmp_path, sources)
    assert result.returncode == 2 and not result.stdout
    assert "batch geometry budget" in result.stderr


def test_floor_grid_through_vertices_partitions_reversed_boundary(tmp_path):
    source = floor([line(60, "grid", [2, 1], [6, 3])], [[0, 4], [8, 4], [8, 0], [0, 0]])
    result, _ = run(tmp_path, [source])
    assert result.returncode == 0, result.stderr
    split = json.loads(result.stdout)["findings"][0]
    assert split["seams"] == [{"cut_id": "60", "segments_xy_ft": [[[0, 0], [8, 4]]]}]
    assert [{tuple(point) for point in part["boundary_xy_ft"]} for part in split["pieces"]] == [
        {(0, 0), (8, 4), (0, 4)}, {(0, 0), (8, 4), (8, 0)},
    ]
    assert [part["area_sqft"] for part in split["pieces"]] == [16, 16]


@pytest.mark.parametrize("limit", ["sources", "cuts", "geometry", "bytes"])
def test_limits_never_emit_partial_results(tmp_path, limit):
    wall = {"base_ft": 0, "top_ft": 20}
    if limit == "bytes":
        path = tmp_path / "oversized.json"
        path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
        assert "exceeds 2 MiB" in result.stderr
    elif limit == "sources":
        result, _ = run(tmp_path, [element("wall", i, wall, [level(50, 8)]) for i in range(100, 121)])
        assert "split source elements" in result.stderr
    elif limit == "cuts":
        result, _ = run(tmp_path, [element("wall", 30, wall, [level(i + 50, i + 1) for i in range(11)])])
        assert "cuts per source" in result.stderr
    else:
        sources = [dict(floor([line(60, "grid", [4, 1], [4, 3])], [[i, 0] for i in range(128)]),
                        id=str(i), uid=f"floor-{i}") for i in range(100, 116)]
        result, _ = run(tmp_path, sources)
        assert "batch geometry budget" in result.stderr
    assert result.returncode == 2 and not result.stdout


def test_shared_level_cannot_have_conflicting_snapshot_geometry(tmp_path):
    sources = [element("wall", 30, {"base_ft": 0, "top_ft": 20}, [level(50, 8)]),
               element("structural_column", 31, {"base_ft": 0, "top_ft": 20}, [level(50, 9)])]
    result, _ = run(tmp_path, sources)
    assert result.returncode == 2 and not result.stdout
    assert "conflicting snapshot evidence" in result.stderr
