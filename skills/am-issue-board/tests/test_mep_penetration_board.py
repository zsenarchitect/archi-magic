import json
import subprocess
import sys

import pytest

from test_mep_penetration_preview import evidence, track
from test_structural_compare import SCRIPTS, ib, save_board
from mep_penetration_board import preview_board
from mep_penetration_preview import preview


def test_board_cli_reuses_crossing_after_movement_and_archive(tmp_path, evidence):
    active = tmp_path / "board.json"
    original = ib.plan_create("Fixture", [{"number": 1, "title": "Existing"}])["final_text"]
    save_board(active, original, evidence["host_identity"])
    before = active.read_bytes()
    evidence["hosts"][0]["fire_rating"] = "2 hours"
    key = preview(evidence)["findings"][0]["crossing_key"]
    snapshot = tmp_path / "evidence.json"
    snapshot.write_text(json.dumps(evidence))
    response = subprocess.run([sys.executable, str(SCRIPTS / "mep_penetration_board.py"),
                               "--evidence", str(snapshot), "--board", str(active), "--crossing", key],
                              capture_output=True, text=True)
    assert response.returncode == 0, response.stderr
    assert active.read_bytes() == before
    result = json.loads(response.stdout)
    assert result["additions"] == 1 and result["existing"] == []
    plan = result["plan"]
    assert plan["mep_penetration_target"] == {
        "host_identity": evidence["host_identity"],
        "active": {"id": "40", "unique_id": "board-note", "sha1": ib.text_sha1(original)},
        "archive": None,
    }
    old, added = ib.parse_board(plan["final_text"])["issues"]
    assert old == ib.parse_board(original)["issues"][0]
    assert added["number"] == 2 and added["element_ids"] == [10]
    assert added["findings"] == [
        f"MEP penetration key: {key}",
        "Host id10. Linked run 20. System Supply Air.",
        "Host XYZ in internal feet at the measured crossing is 0.500000, 2.000000, 3.000000.",
        "Host fire rating recorded in Revit is 2 hours.",
    ]

    assert track(evidence) == key
    evidence["pairs"][0]["status"] = "no_crossing"
    moved = preview_board(evidence, [key], active)
    assert "last reviewed crossing" in moved["plan"]["final_text"]
    save_board(active, plan["final_text"], evidence["host_identity"])
    repeated = preview_board(evidence, [key], active)
    assert repeated["plan"] is None
    assert repeated["existing"] == [
        {"crossing_key": key, "number": 2, "archive": False, "observed_status": "out_of_date"},
    ]

    archive = tmp_path / "archive.json"
    archived = "Issue Board Archive\r" + ib.ARCHIVE_RULES + "\r\r"
    archived += "\r".join(row["text"] for row in ib.render_issue(added))
    save_board(archive, archived, evidence["host_identity"], "41", "archive-note")
    with pytest.raises(ValueError, match="overlap"):
        preview_board(evidence, [key], active, archive)
    save_board(active, original, evidence["host_identity"])
    repeated = preview_board(evidence, [key], active, archive)
    assert repeated["plan"] is None
    assert repeated["existing"] == [
        {"crossing_key": key, "number": 2, "archive": True, "observed_status": "out_of_date"},
    ]


@pytest.mark.parametrize("change", ["project", "incomplete", "element_id"])
def test_board_preview_rejects_invalid_evidence(tmp_path, evidence, change):
    key = preview(evidence)["findings"][0]["crossing_key"]
    identity = evidence["host_identity"]
    if change == "project":
        identity = dict(identity, project_uid="other-model")
    elif change == "incomplete":
        evidence["complete"] = False
    else:
        evidence["runs"][0]["system"] = "Supply id42"
    active = tmp_path / "board.json"
    save_board(active, ib.plan_create("Fixture", [])["final_text"], identity)
    errors = {"project": "different saved project", "incomplete": "complete scan",
              "element_id": "MEP system name"}
    with pytest.raises(ValueError, match=errors[change]):
        preview_board(evidence, [key], active)
