import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "delete_approval.py"
RENDERER = SCRIPT.parents[2] / "am-schedule-view" / "scripts" / "render-table.ps1"


@pytest.fixture
def preview():
    notes = [[100 + index, f"Text note column {index + 1}", "Value to remove\nRetained value"]
             for index in range(10)]
    grid = [[200 + index, f"Vertical grid line {index + 1}", "12 feet"] for index in range(11)]
    return {
        "kind": "change-preview",
        "keyColumn": "ElementId",
        "columns": ["ElementId", "Element", "Value"],
        "before": notes + grid,
        "after": [[key, label, "Retained value"] for key, label, _ in notes]
                 + [[key, label, "11 feet"] for key, label, _ in grid],
        "deletions": [
            {"id": 300, "category": "Shared Parameters", "name": "Unused room parameter"},
            {"id": "301", "category": "Detail Lines", "name": "Horizontal grid line under the removed row"},
        ],
    }


def run_cli(tmp_path, preview, run=None):
    preview_file = tmp_path / "preview.json"
    preview_file.write_text(json.dumps(preview), encoding="utf-8")
    command = [sys.executable, str(SCRIPT), str(preview_file)]
    if run is not None:
        dry_run = tmp_path / "run.json"
        dry_run.write_text(json.dumps({"response": run}), encoding="utf-8")
        command.extend(["--dry-run", str(dry_run)])
    return subprocess.run(command, capture_output=True, text=True)


def test_preview_lists_both_deletes_separately_from_twenty_one_edits(tmp_path, preview):
    completed = run_cli(tmp_path, preview)
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [
        "Delete: 2 elements",
        "- ID 300 | Shared Parameters | Unused room parameter",
        "- ID 301 | Detail Lines | Horizontal grid line under the removed row",
    ]
    assert [row[0] for row in preview["before"]] == [*range(100, 110), *range(200, 211)]
    assert [row[0] for row in preview["after"]] == [*range(100, 110), *range(200, 211)]


@pytest.mark.parametrize("corruption", [None, "same_count_wrong_ids", "missing", "duplicated", "truncated", "another_model"])
def test_dry_run_approval_requires_exact_deletion_ids(tmp_path, preview, corruption):
    run = {"mode": "dry_run", "status": "ok", "decision": "rolled_back", "result_truncated": False,
           "changes": {"deleted_count": 2, "deleted": [300, "301"]}}
    changes = run["changes"]
    if corruption == "same_count_wrong_ids":
        changes["deleted"][1] = "999"
    elif corruption == "missing":
        changes["deleted"].pop()
    elif corruption == "duplicated":
        changes["deleted"][1] = 300
    elif corruption == "truncated":
        run["result_truncated"] = True
    elif corruption == "another_model":
        changes["other_documents"] = [{"deleted_count": 1}]
    completed = run_cli(tmp_path, preview, run)
    assert completed.returncode == (2 if corruption else 0)
    if corruption == "same_count_wrong_ids":
        assert "Extra IDs: 999. Missing IDs: 301." in completed.stderr


def test_accepts_named_deletion_rows_in_saved_evidence(tmp_path, preview):
    run = {"mode": "dry_run", "status": "ok", "decision": "rolled_back", "result_truncated": False,
           "changes": {"deleted_count": 2, "deleted": [{"id": 300}, {"id": "301"}]}}
    assert run_cli(tmp_path, preview, run).returncode == 0


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="The HTML renderer requires PowerShell.")
def test_renderer_preserves_exact_int64_ids_and_inert_labels(tmp_path, preview):
    class DeletionData(HTMLParser):
        def __init__(self):
            super().__init__()
            self.reading = False
            self.payload = []

        def handle_starttag(self, tag, attrs):
            self.reading = tag == "script" and dict(attrs).get("id") == "deletions"

        def handle_endtag(self, tag):
            self.reading = False

        def handle_data(self, data):
            if self.reading:
                self.payload.append(data)

    preview["deletions"][1].update(id=9007199254740993, name="</script><img src=x> __DATA_JSON__")
    source = tmp_path / "preview.json"
    target = tmp_path / "preview.html"
    source.write_text(json.dumps(preview), encoding="utf-8")
    run = subprocess.run(["pwsh", "-NoProfile", "-File", str(RENDERER),
                          "-Json", str(source), "-Out", str(target)], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    parser = DeletionData()
    parser.feed(target.read_text(encoding="utf-8"))
    assert json.loads("".join(parser.payload)) == [
        dict(row, id=str(row["id"])) for row in preview["deletions"]
    ]
