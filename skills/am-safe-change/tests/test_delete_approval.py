import hashlib
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


def run_cli(tmp_path, preview, run=None, *, approved_digest=None):
    preview_file = tmp_path / "preview.json"
    preview_file.write_text(json.dumps(preview), encoding="utf-8")
    command = [sys.executable, str(SCRIPT), str(preview_file)]
    if run is not None:
        dry_run = tmp_path / "run.json"
        dry_run.write_text(json.dumps({"response": run}), encoding="utf-8")
        digest = approved_digest if approved_digest is not None else hashlib.sha256(preview_file.read_bytes()).hexdigest()
        command.extend(["--dry-run", str(dry_run)])
        if digest:
            command.extend(["--approved-preview-sha256", digest])
    return subprocess.run(command, capture_output=True, text=True)


def test_preview_lists_both_deletes_separately_from_twenty_one_edits(tmp_path, preview):
    completed = run_cli(tmp_path, preview)
    assert completed.returncode == 0
    assert completed.stdout.splitlines()[:-1] == [
        "Delete: 2 elements",
        "- ID 300 | Shared Parameters | Unused room parameter",
        "- ID 301 | Detail Lines | Horizontal grid line under the removed row",
    ]
    digest = hashlib.sha256((tmp_path / "preview.json").read_bytes()).hexdigest()
    assert completed.stdout.splitlines()[-1] == f"Preview SHA-256: {digest}"
    assert [row[0] for row in preview["before"]] == [*range(100, 110), *range(200, 211)]
    assert [row[0] for row in preview["after"]] == [*range(100, 110), *range(200, 211)]


@pytest.mark.parametrize("corruption", [
    None, "same_count_wrong_ids", "missing", "duplicated", "truncated", "another_model",
    "missing_run_id", "failed_script", "approval", "warnings", "dialogs", "closed_documents",
])
def test_dry_run_approval_requires_exact_deletion_ids(tmp_path, preview, corruption):
    run = {"mode": "dry_run", "status": "ok", "decision": "rolled_back", "result_truncated": False,
           "run_id": "fixture-dry-run", "exit_code": 0,
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
    elif corruption == "missing_run_id":
        del run["run_id"]
    elif corruption == "failed_script":
        run["exit_code"] = 1
    elif corruption == "approval":
        run["approval"] = "auto"
    elif corruption in ("warnings", "dialogs", "closed_documents"):
        run[corruption] = ["Unexpected activity"]
    completed = run_cli(tmp_path, preview, run)
    assert completed.returncode == (2 if corruption else 0)
    if corruption == "same_count_wrong_ids":
        assert "Extra IDs: 999. Missing IDs: 301." in completed.stderr


@pytest.mark.parametrize("include_approval", [False, True])
def test_dry_run_refuses_a_missing_or_changed_preview_approval(tmp_path, preview, include_approval):
    run = {"mode": "dry_run", "status": "ok", "decision": "rolled_back", "result_truncated": False,
           "run_id": "fixture-dry-run", "exit_code": 0,
           "changes": {"deleted_count": 2, "deleted": [300, "301"]}}
    original = json.dumps(preview).encode("utf-8")
    approved_digest = hashlib.sha256(original).hexdigest() if include_approval else ""
    if include_approval:
        preview["after"][0][2] = "Changed after approval"
    completed = run_cli(tmp_path, preview, run, approved_digest=approved_digest)
    assert completed.returncode == 2
    assert completed.stdout == ""
    message = "differs from the approved file" if include_approval else "recorded when the user approved"
    assert message in completed.stderr


def test_accepts_named_deletion_rows_in_saved_evidence(tmp_path, preview):
    run = {"mode": "dry_run", "status": "ok", "decision": "rolled_back", "result_truncated": False,
           "run_id": "fixture-dry-run", "exit_code": 0,
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
