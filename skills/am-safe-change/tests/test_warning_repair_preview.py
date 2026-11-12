import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import warning_repair_preview as repair

DUPLICATE = "11111111-0000-4000-8000-000000000001"
SEPARATOR = "11111111-0000-4000-8000-000000000002"
OTHER = "11111111-0000-4000-8000-000000000003"


@pytest.fixture
def snapshot():
    furniture = {"recipe": "duplicate-furniture", "eligible": True, "writable": True,
                 "equivalence": {"symbol": "7", "phase": "8", "transform": [1, 2, 3]}}
    line = {"recipe": "coincident-room-separators", "eligible": True, "writable": True,
            "equivalence": {"level": "9", "endpoints": [[0, 0, 0], [1, 0, 0]]}}
    return {
        "kind": "warning-repair-snapshot", "complete": True,
        "document": {"project_uid": "fixture", "path_sha256": "a" * 64},
        "request": {"pairs": [{"keep_id": "1", "remove_id": "2", "failure_id": DUPLICATE},
                              {"keep_id": "3", "remove_id": "4", "failure_id": SEPARATOR}]},
        "repair_types": {DUPLICATE: "duplicate-furniture", SEPARATOR: "coincident-room-separators"},
        "warnings": [[DUPLICATE, ["2", "1"], []], [DUPLICATE, ["6", "5"], []],
                     [SEPARATOR, ["3", "4"], []], [OTHER, ["7"], ["8"]]],
        "elements": {"1": dict(furniture, uid="furniture-1"), "2": dict(furniture, uid="furniture-2"),
                     "3": dict(line, uid="separator-3"), "4": dict(line, uid="separator-4")},
        "room_inventory_complete": True,
    }


def test_cli_plans_only_the_explicit_removals_and_exact_warning_delta(snapshot, tmp_path):
    raw, machine, shown = (tmp_path / name for name in ("record.json", "plan.json", "preview.json"))
    raw.write_text(json.dumps({"result": {"status": "ok", "snapshot_json": json.dumps(snapshot)}}))
    subprocess.run([sys.executable, str(SCRIPTS / "warning_repair_preview.py"),
                    str(raw), str(machine), str(shown)], check=True)
    plan, display = json.loads(machine.read_text()), json.loads(shown.read_text())
    assert plan["expected_warnings"] == [[DUPLICATE, ["5", "6"], []], [OTHER, ["7"], ["8"]]]
    assert [(step["keep_uid"], step["remove_uid"]) for step in plan["steps"]] == [
        ("furniture-1", "furniture-2"), ("separator-3", "separator-4"),
    ]
    assert display["ownership_ids"] == ["2", "4"]
    assert display["before"][-2][-1] == 2 and display["after"][-2][-1] == 1
    assert display["before"][-1][-1] == 1 and display["after"][-1][-1] == 0
    assert display["review_only"] and display["steps"] == []
    assert display["basis_sha256"] == plan["basis_sha256"]


@pytest.mark.parametrize("problem", ["additional", "ambiguous", "changed_element", "borrowed", "incomplete_rooms"])
def test_one_blocked_pair_prevents_all_repairs(snapshot, problem):
    if problem == "additional":
        snapshot["warnings"][0][2] = ["2"]
    elif problem == "ambiguous":
        snapshot["warnings"].append([OTHER, ["99"], ["2"]])
    elif problem == "changed_element":
        snapshot["elements"]["2"]["equivalence"] = {"symbol": "10"}
    elif problem == "borrowed":
        snapshot["elements"]["2"]["writable"] = False
    else:
        snapshot["room_inventory_complete"] = False
    plan, display = repair.compute(snapshot)
    assert plan["blocked"] and display["blocked"] and not plan["steps"]
    assert repair.warning_multiset(plan["expected_warnings"]) == repair.warning_multiset(snapshot["warnings"])
    assert display["warnings"]


def test_keep_remove_ids_cannot_be_reused_in_another_pair(snapshot):
    snapshot["request"]["pairs"][1]["keep_id"] = "2"
    with pytest.raises(ValueError, match="disjoint"):
        repair.compute(snapshot)
