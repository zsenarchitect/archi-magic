import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "room_views_preview.py"
sys.path.insert(0, str(SCRIPT.parent))
import room_views_preview as preview


def identity(number, versioned=False):
    element = {"id": str(number), "uid": "fixture-%s" % number}
    if versioned:
        element["version"] = "fixture-version"
    return element


def snapshot():
    return {
        "document_identity": {"project_uid": "fixture-project", "path_sha256": "a" * 64},
        "view_names": ["Existing View"],
        "view_names_complete": True,
        "padding_feet": 0.25,
        "rooms": [{
            **identity(9007199254740993),
            "number": "101",
            "level": identity(2),
            "phase": identity(3),
            "design_option": None,
            "host_plan": identity(4, True),
            "families": {
                "elevation": {"type": identity(5, True), "template": identity(6, True)},
                "section": {"type": identity(7, True), "template": identity(8, True)},
            },
            "geometry_status": "prismatic_finish_verified",
            "finish_sources": [identity(number) for number in range(9, 13)],
            "footprint": [[0, 0], [10, 0], [10, 6], [0, 6]],
            "heights": [12, 20],
            "requests": [
                {"kind": "elevation", "side": side} for side in range(4)
            ] + [{"kind": "section", "cut": [[2, -1], [2, 7]], "look": [1, 0]}],
        }],
    }


def test_orthogonal_room_and_asymmetric_section():
    data = snapshot()
    result = preview.compute(data)
    assert result["kind"] == "change-preview"
    assert result["review_only"] is True
    assert result["steps"] == []
    assert result["blocked"] is False
    assert result["proposal"]["snapshot_sha256"] == hashlib.sha256(preview.encode(data)).hexdigest()
    assert result["proposal_sha256"] == hashlib.sha256(preview.encode(result["proposal"])).hexdigest()
    views = result["proposal"]["views"]
    assert [view["look"] for view in views] == [
        [0, -1, 0], [1, 0, 0], [0, 1, 0], [-1, 0, 0], [1, 0, 0],
    ]
    south, east, _, _, section = views
    assert south["name"] == "101 Interior Elevation 01"
    assert south["room_id"] == "9007199254740993"
    assert south["origin"] == [5, 3, 16]
    assert south["crop_min"] == [-5.25, -4.25]
    assert south["crop_max"] == [5.25, 4.25]
    assert south["far_extent"] == 3.25
    assert east["crop_max"] == [3.25, 4.25]
    assert east["far_extent"] == 5.25
    assert section["name"] == "101 Interior Section 01"
    assert section["origin"] == [2, 3, 16]
    assert section["far_extent"] == 8.25
    assert section["crop_max"] == [3.25, 4.25]
    assert section["template"]["id"] == "8"
    data["rooms"][0]["requests"][-1]["look"] = [-1, 0]
    reversed_section = preview.compute(data)["proposal"]["views"][-1]
    assert reversed_section["far_extent"] == 2.25


def test_rotated_clockwise_room_preserves_world_facing_and_dimensions():
    data = snapshot()
    room = data["rooms"][0]
    root = math.sqrt(0.5)
    room["footprint"] = [
        [100 + (x - y) * root, 200 + (x + y) * root]
        for x, y in reversed(room["footprint"])
    ]
    room["requests"] = [{"kind": "elevation", "side": 2}]
    view, = preview.compute(data)["proposal"]["views"]
    assert view["look"] == pytest.approx([root, -root, 0])
    assert view["origin"] == pytest.approx([100 + 2 * root, 200 + 8 * root, 16])
    assert view["crop_max"] == pytest.approx([5.25, 4.25])
    assert view["far_extent"] == pytest.approx(3.25)


def test_skewed_room_marker_and_oblique_section():
    data = snapshot()
    room = data["rooms"][0]
    room["footprint"] = [[0, 0], [10, 0], [13, 6], [3, 6]]
    room["requests"] = [
        {"kind": "elevation", "side": 1},
        {"kind": "section", "cut": [[-2, 3], [15, 3]], "look": [0, 1]},
    ]
    elevation, section = preview.compute(data)["proposal"]["views"]
    assert elevation["look"] == pytest.approx([2 / math.sqrt(5), -1 / math.sqrt(5), 0])
    assert elevation["origin"] == pytest.approx([8.5, 4.5, 16])
    assert elevation["crop_max"] == pytest.approx([1.5 * math.sqrt(5) + 0.25, 4.25])
    assert elevation["far_extent"] == pytest.approx(1.5 * math.sqrt(5) + 0.25)
    assert section["origin"] == [6.5, 3, 16]
    assert section["crop_max"] == [5.25, 4.25]
    assert section["far_extent"] == 3.25


def test_equivalent_section_is_deduplicated_and_opposite_facing_is_distinct():
    data = snapshot()
    requests = data["rooms"][0]["requests"]
    requests.extend([
        {"kind": "section", "cut": [[2, 9], [2, -2]], "look": [2, 0]},
        {"kind": "section", "cut": [[2, 9], [2, -2]], "look": [-2, 0]},
    ])
    assert [row[0] for row in preview.compute(data)["after"]][-2:] == [
        "101 Interior Section 01", "101 Interior Section 02",
    ]


@pytest.mark.parametrize(("change", "reason"), [
    (lambda data: data["view_names"].append("101 interior elevation 01"), "already exists"),
    (lambda data: data["rooms"][0].update(footprint=[[0, 0], [10, 0], [2, 2], [0, 6]]), "convex"),
    (lambda data: data["rooms"][0]["requests"][-1].update(cut=[[2, 0], [2, 4]]), "complete room slice"),
    (lambda data: data["rooms"][0]["requests"][-1].update(look=[1, 1]), "perpendicular"),
    (lambda data: data["rooms"][0].update(heights=[20, 20]), "positive room height"),
    (lambda data: data["rooms"][0]["families"]["section"]["template"].pop("version"), "required version"),
])
def test_unqualified_data_blocks_preview(change, reason):
    data = snapshot()
    change(data)
    result = preview.compute(data)
    assert result["blocked"] is True
    assert result["steps"] == []
    assert any(reason in warning["text"] for warning in result["warnings"])


def test_native_inventory_exact_limit_and_one_over():
    data = snapshot()
    data["view_names"] = ["View %d" % number for number in range(preview.MAX_NAME_VISITS)]
    assert preview.compute(data)["blocked"] is False
    data["view_names"].append("One more view")
    with pytest.raises(ValueError, match="View names"):
        preview.compute(data)


def test_command_writes_standard_preview_and_returns_blocked_status(tmp_path):
    data = snapshot()
    data["view_names"].append("101 Interior Elevation 01")
    source = tmp_path / "room.json"
    target = tmp_path / "preview.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--snapshot", str(source), "--out", str(target)],
        capture_output=True, check=False,
    )
    assert run.returncode == 1, run.stderr
    assert json.loads(target.read_text(encoding="utf-8")) == preview.compute(data)
