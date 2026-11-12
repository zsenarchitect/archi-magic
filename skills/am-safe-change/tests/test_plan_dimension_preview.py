import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from plan_dimension_preview import compute


def witness(id_value, station, *, reference=None, trace=None):
    return {
        "source_id": str(id_value),
        "source_uid": "source-" + str(id_value),
        "stable_ref": reference or "ref-" + str(id_value),
        "trace": trace if trace is not None else [[station, -5, 0], [station, 5, 0]],
    }


def request(key="grid-a", mode="grid", witnesses=None, line=None):
    return {
        "key": key, "mode": mode,
        "witnesses": witnesses if witnesses is not None else [witness(12, 7), witness(10, 0), witness(11, 3)],
        "line": line if line is not None else [[-1, 0, 0], [9, 0, 0]],
    }


def basis(*requests, existing=(), frame=None):
    return {
        "kind": "plan-dimension-snapshot", "version": 1,
        "project": {"project_uid": "project", "path_sha256": "0" * 64},
        "view": {"id": "41", "uid": "view-a", "frame": frame or [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]},
        "dimension_type": {"id": "51", "uid": "linear-style"},
        "existing_complete": True, "existing": list(existing), "requests": list(requests),
        "short_curve_tolerance_ft": 0.0026,
    }


def test_unequal_rotated_grid_bays_use_signed_stations():
    angle = math.radians(37)
    c, s = math.cos(angle), math.sin(angle)

    def transform(point):
        x, y, z = point
        return [100 + c * x - s * y, 200 + s * x + c * y, 15 + z]

    rotated = request()
    rotated["line"] = list(map(transform, rotated["line"]))
    for item in rotated["witnesses"]:
        item["trace"] = list(map(transform, item["trace"]))
    frame = [transform([0, 0, 0]), [c, s, 0], [-s, c, 0], [0, 0, 1]]
    preview, proposals = compute(basis(rotated, frame=frame))
    item = proposals["grid-a"]
    assert [row["source_id"] for row in item["witnesses"]] == ["10", "11", "12"]
    assert [row["station_ft"] for row in item["witnesses"]] == pytest.approx([1, 4, 8])
    assert item["segments_ft"] == pytest.approx([3, 4])
    assert preview["ownership_ids"] == ["41"]
    assert preview["steps"] == [] and preview["review_only"] is True


def test_wall_span_and_mirrored_opening_use_actual_geometry():
    overall = request("wall-span", "overall", [witness(21, 8), witness(20, 1)])
    opening = request("window", "opening", [witness(22, 6, reference="left"), witness(22, 2, reference="right")])
    preview, proposals = compute(basis(overall, opening))
    assert not preview["blocked"]
    assert proposals["wall-span"]["segments_ft"] == [7]
    assert proposals["window"]["segments_ft"] == [4]
    assert [row["stable_ref"] for row in proposals["window"]["witnesses"]] == ["right", "left"]


@pytest.mark.parametrize(("line", "message"), [
    ([[-1, -1, 0], [9, 1, 0]], "perpendicular"),
    ([[-1, 0, 0.1], [9, 0, 0.1]], "outside"),
    ([[1, 0, 0], [9, 0, 0]], "does not span"),
    ([[-1, 6, 0], [9, 6, 0]], "misses"),
])
def test_intersection_alone_does_not_make_a_valid_dimension(line, message):
    preview, proposals = compute(basis(request(line=line)))
    assert preview["blocked"] and proposals == {}
    assert preview["warnings"][0]["row"] == "grid-a"
    assert message in preview["warnings"][0]["text"]


def test_unresolved_native_references_remain_visible_and_block_the_batch():
    missing = request("door", "opening", [])
    missing["unresolved"] = [{"source_id": "93", "reason": "Missing left reference"}]
    preview, proposals = compute(basis(request(), missing))
    assert preview["after"][1][:3] == ["door", "opening", "Blocked"]
    assert preview["warnings"][0]["text"] == "93: Missing left reference"
    assert preview["ownership_ids"] == []
    assert proposals == {}


def test_reverse_native_string_on_unbounded_support_is_unchanged():
    existing = {
        "type_id": "51",
        "references": ["ref-12", "ref-11", "ref-10"],
        "segments_ft": [4, 3],
        "support": {"point": [30, 0, 0], "direction": [-1, 0, 0]},
    }
    preview, proposals = compute(basis(request(), existing=[existing]))
    assert preview["after"] == [["grid-a", "grid", "Already exists", 3, "3, 4"]]
    assert preview["after"] == preview["before"]
    assert proposals == {}

    existing["segments_ft"] = [3, 4]
    preview, proposals = compute(basis(request(), existing=[existing]))
    assert preview["blocked"] and proposals == {}


def test_reversed_request_coalesces_but_conflicting_chain_blocks():
    first = request()
    reversed_request = request("reversed", line=list(reversed(first["line"])))
    preview, proposals = compute(basis(first, reversed_request))
    assert [row[2] for row in preview["after"]] == ["Proposed", "Coalesced"]
    assert list(proposals) == ["grid-a"]

    overlap = request("overlap", witnesses=[witness(10, 0), witness(12, 7)])
    preview, proposals = compute(basis(first, overlap))
    assert preview["blocked"] and proposals == {}


def test_cli_stops_when_the_inventory_is_incomplete(tmp_path):
    snapshot = basis(request())
    snapshot["existing_complete"] = False
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    result = subprocess.run([sys.executable, str(SCRIPTS / "plan_dimension_preview.py"), str(path)],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "complete reviewed dimension snapshot" in result.stderr


def test_placement_line_is_snapped_into_the_verified_plane():
    near_plane = request(line=[[-1, 0, 0.0004], [9, 0, -0.0004]])
    _, proposals = compute(basis(near_plane))
    assert proposals["grid-a"]["line"] == [[-1, 0, 0], [9, 0, 0]]
