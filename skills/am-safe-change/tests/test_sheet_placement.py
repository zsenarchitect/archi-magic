"""Check repeat anchors and geometry using measured-sheet fixtures."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sheet_placement_preview as preview


def scope(sheet_id, usable):
    return {"usable_ft": usable, "title_block_id": str(int(sheet_id) + 20),
            "title_block_uid": f"ab-{sheet_id}", "revision_sha256": "b" * 64}


def sheet(sheet_id, number, outline, measured):
    return {**{key: value for key, value in measured.items() if key != "usable_ft"},
            "id": sheet_id, "uid": f"aa-{sheet_id}", "number": number, "outline_ft": outline,
            "ownership_ok": True, "placements": [], "obstacles": []}


def view(view_id, kind):
    return {"id": view_id, "uid": f"ac-{view_id}", "name": kind.title(), "kind": kind,
            "eligible": True, "saved_position": False, "placed_sheet_ids": []}


def snapshot():
    scopes = {"10": scope("10", [0, 0, 4, 3]), "11": scope("11", [0.2, 0.1, 3, 2])}
    return {
        "kind": "sheet-placement-snapshot", "complete": True,
        "document": {"project_uid": "aa-01", "path_sha256": "a" * 64},
        "request": {"clearance_ft": 0.05, "sheets": scopes, "placements": [
            {"view_id": "90", "sheet_ids": ["10", "11"], "point_ft": [1, 1]},
            {"view_id": "91", "sheet_ids": ["10", "11"], "point_ft": [2, 1]},
        ]},
        "views": [view("90", "legend"), view("91", "schedule")],
        "sheets": [sheet("10", "A101", [0, 0, 5, 3.5], scopes["10"]),
                   sheet("11", "A102", [0.1, 0.05, 3.5, 2.5], scopes["11"])],
        "viewport_eligible_pairs": [["10", "90"], ["11", "90"]],
    }


def measured(step, identifier, shapes):
    return {**step, "id": identifier, "uid": f"ad-{identifier}", "rectangles_ft": shapes}


def test_repeat_uses_same_absolute_anchor_in_preview_cli(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot()))
    plan = json.loads(subprocess.check_output([sys.executable, SCRIPTS / "sheet_placement_preview.py", path]))
    assert not plan["blocked"]
    assert [(row["sheet_id"], row["kind"], row["point_ft"]) for row in plan["steps"]] == [
        ("10", "legend", [1, 1]), ("11", "legend", [1, 1]),
        ("10", "schedule", [2, 1]), ("11", "schedule", [2, 1]),
    ]
    assert plan["ownership_ids"] == ["10", "11"]


def test_measured_geometry_requires_label_clearance_and_all_sheets_to_fit():
    data = snapshot()
    data["sheets"][1]["obstacles"] = [{"id": "32", "rectangles_ft": [[1.25, 0.5, 1.40, 0.8]]}]
    plan = preview.compute(data)
    shapes = [[0.8, 0.9, 1.2, 1.15], [0.8, 0.7, 1.2, 0.9]]
    placed = [
        measured(plan["steps"][0], "100", shapes),
        measured(plan["steps"][1], "101", shapes),
        measured(plan["steps"][2], "102", [[1.9, 0.65, 2.6, 1.1]]),
        measured(plan["steps"][3], "103", [[1.9, 0.65, 2.6, 1.1]]),
    ]
    assert preview.verify_layout(plan, data, placed) == placed
    placed[1] = {**placed[1], "rectangles_ft": [shapes[0], [0.8, 0.7, 1.201, 0.9]]}
    with pytest.raises(ValueError, match="overlaps"):
        preview.verify_layout(plan, data, placed)
    placed[1] = {**placed[1], "rectangles_ft": shapes}
    placed[-1] = {**placed[-1], "rectangles_ft": [[1.9, 0.65, 3.01, 1.1]]}
    with pytest.raises(ValueError, match="exceeds"):
        preview.verify_layout(plan, data, placed)


def test_same_pair_at_the_correct_anchor_is_a_noop_and_wrong_anchor_blocks():
    data = snapshot()
    for i, sheet_row in enumerate(data["sheets"]):
        sheet_row["placements"] = [
            {"id": str(101 + 2 * i), "uid": f"ad-{101 + 2*i}", "view_id": "90",
             "kind": "legend", "point_ft": [1, 1], "rectangles_ft": [[0.8, 0.9, 1.2, 1.1]]},
            {"id": str(102 + 2 * i), "uid": f"ad-{102 + 2*i}", "view_id": "91",
             "kind": "schedule", "point_ft": [2, 1], "rectangles_ft": [[1.9, 0.65, 2.6, 1.1]]},
        ]
    for item in data["views"]:
        item["placed_sheet_ids"] = ["10", "11"]
    data["viewport_eligible_pairs"] = []
    plan = preview.compute(data)
    assert plan["steps"] == [] and not plan["blocked"] and plan["ownership_ids"] == []
    assert all(row[-1] == "Already placed" for row in plan["after"])
    data["sheets"][1]["placements"][0]["point_ft"] = [1.01, 1]
    plan = preview.compute(data)
    assert plan["blocked"] and plan["after"][1][4] == [1.01, 1] and plan["steps"] == []


@pytest.mark.parametrize("mutation", ["saved_position", "ordinary_repeat", "other_sheet", "ownership"])
def test_blocks_unsupported_or_unowned_placements(mutation):
    data = snapshot()
    if mutation == "saved_position":
        data["views"][0]["saved_position"] = True
    elif mutation in ("ordinary_repeat", "other_sheet"):
        data["views"][0]["kind"] = "view"
        if mutation == "other_sheet":
            data["views"][0]["placed_sheet_ids"] = ["14"]
            data["request"]["placements"][0]["sheet_ids"] = ["10"]
            data["viewport_eligible_pairs"] = [["10", "90"]]
    else:
        data["sheets"][1]["ownership_ok"] = False
    result = preview.compute(data)
    assert result["blocked"] and result["steps"] == [] and result["ownership_ids"] == []


def test_layout_stops_when_reviewed_sheet_revision_state_has_changed():
    data = snapshot()
    data["sheets"][1]["revision_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="revision state"):
        preview.compute(data)
