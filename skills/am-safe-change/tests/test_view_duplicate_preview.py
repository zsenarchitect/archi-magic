import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import view_duplicate_preview as preview


@pytest.fixture
def snapshot():
    primary = {
        "name": "Étage {index}", "uid": "primary", "primary_id": "-1",
        "allowed_modes": ["plain", "detailing", "dependent"], "template_allowed": True,
        "details_complete": True, "details": [{"id": "31", "position": [1, 2, 3]}],
        "template_id": "7", "template_name": "Existing", "sheets": [], "controlled_settings": ["Scale"],
    }
    dependent = dict(primary, name="Étage nord", uid="dependent", primary_id="10", sheets=["A101"])
    return {
        "kind": "view-duplicate-snapshot", "complete": True, "document": {"project_uid": "project"},
        "request": {
            "sources": [{"id": "10", "mode": "plain"}, {"id": "10", "mode": "detailing"},
                        {"id": "11", "mode": "dependent"}],
            "template_id": "8", "pattern": "{name}  – {index}", "allow_family_template_change": True,
        },
        "template": {"id": "8", "uid": "template", "name": "Proposed", "controlled_settings": ["Scale", "Detail level"]},
        "names": ["Étage {index}", "Étage nord", "Existing", "Proposed"],
        "sources": {"10": primary, "11": dependent},
        "families": {"10": {"template_allowed": True, "members": {"10": primary, "11": dependent}}},
    }


def test_plan_and_display_from_full_runtime_record(snapshot, tmp_path):
    record = tmp_path / "run.json"
    plan_path = tmp_path / "plan.json"
    preview_path = tmp_path / "preview.json"
    record.write_text(json.dumps({"result": {"status": "ok", "snapshot_json": json.dumps(snapshot)}}))
    subprocess.run([sys.executable, str(SCRIPTS / "view_duplicate_preview.py"),
                    str(record), str(plan_path), str(preview_path)], check=True)
    plan, display = map(lambda path: json.loads(path.read_text()), (plan_path, preview_path))
    assert plan["steps"] == [
        {"source_id": "10", "source_uid": "primary", "mode": "plain", "name": "Étage {index}  – 1", "primary_id": "-1"},
        {"source_id": "10", "source_uid": "primary", "mode": "detailing", "name": "Étage {index}  – 2", "primary_id": "-1"},
        {"source_id": "11", "source_uid": "dependent", "mode": "dependent", "name": "Étage nord  – 3", "primary_id": "10"},
    ]
    assert plan["family_changes"] == {"10": ["10", "11"]}
    assert display["ownership_ids"] == plan["ownership_ids"] == ["10", "11"]
    assert display["basis_sha256"] == plan["basis_sha256"]
    assert display["review_only"] and display["steps"] == []
    assert display["before"][-1][-3:] == ["Existing", "A101", "Scale"]
    assert display["after"][-1][-3:] == ["Proposed", "A101", "Scale, Detail level"]


@pytest.mark.parametrize("failure", ["name", "batch", "family", "primary", "detail"])
def test_one_blocked_row_prevents_all_steps(snapshot, failure):
    if failure == "name":
        snapshot["names"].append("ÉTAGE {INDEX}  – 1")
    elif failure == "batch":
        snapshot["request"]["pattern"] = "{name} Copy"
    elif failure == "family":
        snapshot["request"]["allow_family_template_change"] = False
    elif failure == "primary":
        snapshot["families"]["10"]["template_allowed"] = False
    else:
        snapshot["sources"]["10"]["details_complete"] = False
    plan, display = preview.compute(snapshot)
    assert plan["blocked"] and display["blocked"]
    assert plan["steps"] == []
    assert plan["family_changes"] == {}
    assert any(warning["blocking"] for warning in display["warnings"])


def test_unchanged_family_only_checks_primary_ownership(snapshot):
    for member in snapshot["families"]["10"]["members"].values():
        member["template_id"] = "8"
    snapshot["request"]["allow_family_template_change"] = False
    plan, display = preview.compute(snapshot)
    assert plan["family_changes"] == {}
    assert display["ownership_ids"] == ["10"]
    assert not plan["blocked"]


def test_unsaved_detail_change_changes_plan_basis(snapshot):
    original, _ = preview.compute(snapshot)
    moved = copy.deepcopy(snapshot)
    moved["sources"]["10"]["details"][0]["position"] = [4, 5, 6]
    updated, _ = preview.compute(moved)
    assert original["steps"] == updated["steps"]
    assert original["basis_sha256"] != updated["basis_sha256"]


@pytest.mark.parametrize("pattern", ["{name:}", "{unknown}", "{{name}}", "{name", "{name} "])
def test_malformed_name_patterns_are_rejected(pattern):
    with pytest.raises(ValueError):
        preview.duplicate_name(pattern, "Source", 1)
