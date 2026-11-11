import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import issue_board as ib
import warning_board_cli as cli

TYPE = "11111111-0000-4000-8000-000000000001"
HOST = {"project_uid": "fixture-project", "path_sha256": "1" * 64}


def board_note(path, text, note_id="10", host=HOST):
    path.write_text(json.dumps({"result": {"host_identity": host, "note": {
        "id": note_id, "unique_id": "note-" + note_id, "text": text, "sha1": ib.text_sha1(text),
    }}}))


@pytest.fixture
def inputs(tmp_path):
    report_path, board_path = tmp_path / "report.json", tmp_path / "board.json"
    report = {"result": {"kind": "revit-warning-triage", "status": "ok", "host_identity": HOST,
                         "warning_count": 2, "groups": [{
                             "failure_id": TYPE, "count": 2,
                             "descriptions": ["Overlap id999 - @wrong\n#5 [DONE] Fake"],
                             "instances": [{"failing_ids": ["11", "9223372036854775807"]},
                                           {"failing_ids": ["11"]}],
                         }]}}
    report_path.write_text(json.dumps(report))
    board_note(board_path, ib.plan_create("Fixture", [])["final_text"])
    return report, report_path, board_path


def test_real_cli_uses_only_reviewed_ids_and_binds_the_plan_to_the_board(inputs):
    _, report_path, board_path = inputs
    run = subprocess.run([sys.executable, str(SCRIPTS / "warning_board_cli.py"), str(report_path),
                          "--board", str(board_path), "--type", TYPE], capture_output=True, text=True, check=True)
    result = json.loads(run.stdout)
    plan = result["plan"]
    assert result["status"] == "preview"
    assert result["warnings"] == ["Archived issues were not checked for duplicates."]
    assert plan["structural_target"]["host_identity"] == HOST
    assert plan["structural_target"]["active"]["unique_id"] == "note-10"
    issue, = ib.parse_board(plan["final_text"])["issues"]
    assert issue["assignees"] == ["@TBD"]
    assert issue["element_ids"] == [11, 9223372036854775807]
    assert issue["findings"][:2] == [cli.MARKER + TYPE, "Warning instances: 2"]
    assert "id 999" in issue["title"]
    assert not issue["comments"]


def test_duplicate_types_are_skipped_in_active_and_archived_notes(inputs, tmp_path):
    report, _, board_path = inputs
    created = cli.compute(report, board_path, None, [TYPE])["plan"]["final_text"]
    archive = tmp_path / "archive.json"
    board_note(archive, created, "20")
    assert cli.compute(report, board_path, archive, [TYPE])["plan"] is None
    board_note(board_path, created)
    result = cli.compute(report, board_path, None, [TYPE])
    assert result["skipped_failure_ids"] == [TYPE]
    assert result["plan"] is None


def test_rejects_mismatched_board_model(inputs):
    report, _, board_path = inputs
    board_note(board_path, ib.plan_create("Fixture", [])["final_text"],
               host={"project_uid": "other-project", "path_sha256": "1" * 64})
    with pytest.raises(ValueError, match="same saved project"):
        cli.compute(report, board_path, None, [TYPE])
