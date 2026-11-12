import json
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "toposolid_preview.py"


def run_preview(tmp_path, mode, snapshot, flags=()):
    source, output = tmp_path / "query.json", tmp_path / "preview.json"
    source.write_text(json.dumps({"result": {"status": "ok", "snapshot_json": json.dumps(snapshot)}}))
    run = subprocess.run([sys.executable, str(SCRIPT), mode, str(source), str(output), *flags],
                         capture_output=True, text=True)
    assert run.returncode in (0, 1), run.stderr
    return run.returncode, json.loads(output.read_text()), json.loads(run.stdout)


def test_alignment_preview_exposes_exact_before_after_and_probe_baseline(tmp_path):
    row = {"point": 1, "requested_xy_ft": [1, 2], "before_ft": [1, 2, 10], "after_ft": [1, 2, 12]}
    snapshot = {
        "toposolid": {"id": "10"},
        "alignment": {"support": {"kind": "edge", "id": "11"}, "rows": [row],
                      "blocked": False, "tolerance_ft": 0.02},
    }
    code, preview, probe = run_preview(tmp_path, "alignment", snapshot)
    assert code == 0 and probe == {"baseline": snapshot}
    assert preview["kind"] == "change-preview" and preview["review_only"]
    assert preview["before"] == [["1", "1, 2", "1, 2, 10"]]
    assert preview["after"] == [["1", "1, 2", "1, 2, 12"]]
    assert preview["ownership_ids"] == ["10"] and not preview["steps"]

    snapshot["alignment"]["rows"].append({"point": 2, "requested_xy_ft": [4, 5], "reason": "ambiguous_vertex"})
    snapshot["alignment"]["blocked"] = True
    code, preview, probe = run_preview(tmp_path, "alignment", snapshot)
    assert code == 1 and probe is None
    assert preview["blocked"] and not preview["ownership_ids"]
    assert preview["after"][1] == ["2", "4, 5", "Excluded"]
    assert preview["warnings"] == [{
        "row": "2", "text": "More than one terrain vertex is within the selected XY tolerance.", "blocking": True,
    }]


def test_floor_preview_lists_the_floor_and_every_derived_edit(tmp_path):
    a, b, c, d = [[x, y, 10] for x, y in ((0, 0), (2, 0), (2, 2), (0, 2))]
    corners = [a, b, c, d]
    peak = [1, 1, 12]
    sides = list(zip(corners, corners[1:] + corners[:1]))
    snapshot = {
        "region": {"id": "10"}, "floor_type": {"id": "11"}, "level": {"id": "12"},
        "level_project_elevation_ft": 10,
        "profile_loops": [[{"kind": "line", "points_ft": [start, end]} for start, end in sides]],
        "top_faces": [{"triangles_ft": [[start, end, peak] for start, end in sides]}],
    }
    flags = ["--floor-offset-ft", "0", "--xy-tolerance-ft", "0.01",
             "--area-tolerance-sqft", "0.1", "--elevation-tolerance-ft", "0.001"]
    code, preview, probe = run_preview(tmp_path, "floor", snapshot, flags)
    assert code == 0 and probe["baseline"] == snapshot
    assert not preview["ownership_ids"] and not preview["steps"]
    assert preview["after"][0] == ["floor", "10", "Candidate floor type 11 on level 12, offset 0.0 ft"]
    assert preview["after"][1] == ["interior:1", "1, 1, 12", "Insert interior vertex XYZ ft"]
    assert probe["shape"]["interior_ft"] == [peak]
    assert probe["shape"]["split_lines_xy_ft"] == [
        [[0, 0], [1, 1]], [[0, 2], [1, 1]], [[1, 1], [2, 0]], [[1, 1], [2, 2]],
    ]
    assert len(preview["after"]) == len(preview["before"]) == 6
    assert probe["elevation_tolerance_ft"] == 0.001
    snapshot["top_faces"][0]["triangles_ft"].pop()
    code, preview, probe = run_preview(tmp_path, "floor", snapshot, flags)
    assert code == 1 and probe is None
    assert preview["blocked"] and preview["warnings"][0]["blocking"]
    assert preview["after"] == [["floor", "10", "Blocked"]]
