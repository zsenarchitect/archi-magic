from copy import deepcopy
import json
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "opening_legend_preview.py"
PLANNER = runpy.run_path(str(SCRIPT))
GRID = {"origin_uv_ft": [0, 0], "columns": 3, "cell_width_ft": 10, "cell_height_ft": 10}


def opening(uid, identifier, mark, category="Doors"):
    return {"uid": uid, "id": identifier, "type_mark": mark, "category": category, "placements": 2,
            "width": {"feet": 3, "display": "3 ft"}, "height": {"feet": 7, "display": "7 ft"},
            "instance_dimensions_vary": {"width": False, "height": False}}


def snapshot():
    return {"kind": "placed-opening-type-snapshot", "scope": "host-project",
            "host_identity": {"project_uid": "fixture-project", "path_sha256": "a" * 64},
            "types": [opening("door", "9007199254740993", "A01"), opening("window", "12", "A01", "Windows")],
            "legend": {"id": "25", "uid": "fixture-legend", "scale": 20, "directions_verified_for_year": True,
                       "origin_ft": [0, 0, 0], "right": [1, 0, 0], "up": [0, 1, 0],
                       "components": [], "text_notes": [], "layout": {
                           "text_type": {"id": "50"}, "target_graphics": [], "sheet_placements": [{"id": "80"}],
                       }}}


def run_cli(tmp_path, state, intent):
    source, request = tmp_path / "read.json", tmp_path / "intent.json"
    source.write_text(json.dumps({"status": "ok", "result": {"status": "ok", "snapshot_json": json.dumps(state)}}))
    request.write_text(json.dumps(intent))
    return subprocess.run([sys.executable, str(SCRIPT), str(source), str(request)], capture_output=True, text=True)


def test_new_legend_outputs_one_row_per_type_and_requests_missing_label(tmp_path):
    state = snapshot()
    state["types"][1]["instance_dimensions_vary"]["height"] = True
    output = run_cli(tmp_path, state, {"grid": GRID})
    assert output.returncode == 1, output.stderr
    blocked = json.loads(output.stdout)
    assert blocked["blocked"] and len(blocked["steps"]) == 2
    assert blocked["ownership_ids"] == ["25"]
    result = run_cli(tmp_path, state, {"grid": GRID, "labels": {"window": "A01 | 3 ft by variable height"}})
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert [(row["type_uid"], row["label"], row["cell_uv_ft"]) for row in preview["steps"]] == [
        ("door", "A01 | 3 ft × 7 ft", [0, -10, 10, 0]),
        ("window", "A01 | 3 ft by variable height", [10, -10, 20, 0]),
    ]
    assert len(preview["warnings"]) == 2 and all(not row["blocking"] for row in preview["warnings"])


def managed_snapshot():
    state = snapshot()
    legend = state["legend"]
    pairs = []
    for slot, row in enumerate(state["types"]):
        offset = slot * 10
        bounds = {"min": [1 + offset, -8, 0], "max": [4 + offset, -1, 0]}
        component = {"id": str(31 + slot * 2), "uid": "component-" + row["uid"], "type_id": row["id"],
                     "type_uid": row["uid"], "direction": "front_elevation", "direction_raw": -7,
                     "opening_category": row["category"], "host_length_ft": None,
                     "position_ft": [2 + offset, -5, 0], "bounds_ft": bounds}
        note = {"id": str(32 + slot * 2), "uid": "note-" + row["uid"], "text": "A01 | 3 ft × 7 ft",
                "position_ft": [2 + offset, -9, 0], "type_id": "50"}
        legend["components"].append(component)
        legend["text_notes"].append(note)
        pair = {"slot": slot, "type_uid": row["uid"]}
        for kind, element in (("component", component), ("note", note)):
            graphic = {"id": element["id"], "uid": element["uid"], "owner_id": "25", "pinned": False, "group_id": "-1",
                       "category_id": "-8", "native_kind": "TextNote" if kind == "note" else "Element",
                       "bounds_ft": bounds}
            if kind == "note":
                graphic["text_appearance"] = {"plain_single_line": True, "leaders": 0, "horizontal_alignment": "Left"}
            legend["layout"]["target_graphics"].append(graphic)
            pair[kind] = PLANNER["paired_state"](element, graphic, kind)
        pairs.append(pair)
    receipt = {"kind": "opening-legend-receipt", "decision": "committed", "approval": "user",
               "host_identity": deepcopy(state["host_identity"]), "legend_uid": legend["uid"], "grid": GRID,
               "run_id": "keep", "readback_run_id": "fresh", "pairs": pairs}
    return state, receipt


def test_refresh_retires_only_receipted_pairs_and_reuses_a_freed_cell():
    state, receipt = managed_snapshot()
    build = PLANNER["build_preview"]
    intent = {"grid": GRID, "receipt": receipt}
    assert build(state, intent)["steps"] == []
    state["types"] = [opening("window", "12", "A02", "Windows"), opening("new", "13", "A03")]
    preview = build(state, intent)
    assert [(row["operation"], row["type_uid"], row["existing_ids"], row["slot"]) for row in preview["steps"]] == [
        ("retire", "door", ["31", "32"], 0), ("add", "new", [], 0), ("refresh", "window", ["33", "34"], 1),
    ]
    assert preview["ownership_ids"] == ["25", "31", "32", "33", "34"]
    with pytest.raises(ValueError, match="verified prior receipt"):
        build(state, {"grid": GRID})
    state["legend"]["text_notes"][1]["text"] = "Edited by hand"
    with pytest.raises(ValueError, match="changed after its kept receipt"):
        build(state, intent)
