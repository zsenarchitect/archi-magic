import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mep_parameter_repair_preview.py"
GUID = "62cba752-ce75-4604-93a8-f58f02909dc7"
LARGE_ID = "9007199254740993"


@pytest.fixture
def inputs(tmp_path):
    host = {"project_uid": "fixture", "path_sha256": "a" * 64}
    evidence = {
        "kind": "mep-parameter-repair-evidence", "version": 1, "guid": GUID,
        "source": {"host_identity": host, "document_identity": host,
                   "link_instance_id": None, "link_instance_uid": None},
        "elements": [
            {"id": LARGE_ID, "uid": "blank-instance", "category_id": "-2008000",
             "parameter_id": "42", "storage": "String", "state": "unset",
             "read_only": False, "has_value": False, "value": None},
            {"id": "22", "uid": "filled-instance", "category_id": "-2008000",
             "parameter_id": "42", "storage": "String", "state": "already_filled",
             "read_only": False, "has_value": True, "value": "Keep this"},
        ],
    }
    receipt = {
        "run_id": "0123456789ab", "mode": "query", "status": "ok", "decision": "rolled_back",
        "exit_code": 0, "result_truncated": False, "error": None,
        "failures": [], "dialogs": [], "blocked": [], "warnings": [],
        "changes": {"added": [], "modified": [], "deleted": [], "added_count": 0,
                    "modified_count": 0, "deleted_count": 0, "by_category": {}, "transactions": []},
    }
    query = tmp_path / "query.json"
    fill = tmp_path / "fill.json"

    def run(text="Ventilation", **overrides):
        result = {"status": "ok", "repair_parameters_json": json.dumps(evidence)}
        query.write_text(json.dumps({"response": dict(receipt, result=result, **overrides)}))
        fill.write_text(json.dumps({"text": text}))
        return subprocess.run([sys.executable, SCRIPT, query, fill], capture_output=True, text=True)

    return evidence, run


def test_preview_shows_only_blank_text_as_a_proposed_change(inputs):
    _, run = inputs
    result = run()
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert preview["kind"] == "change-preview"
    assert preview["review_only"] is True and preview["steps"] == []
    assert preview["source_run_id"] == "0123456789ab"
    assert [row[0] for row in preview["after"]] == [LARGE_ID, "22"]
    assert [row[-1] for row in preview["before"]] == [None, "Keep this"]
    assert [row[-1] for row in preview["after"]] == ["Ventilation", "Keep this"]
    assert "already_filled" in preview["before"][1]
    assert preview["basis_sha256"] != json.loads(run("Revised").stdout)["basis_sha256"]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="The preview renderer needs PowerShell.")
def test_preview_renders_with_exact_revit_ids(inputs, tmp_path):
    _, run = inputs
    result = run()
    assert result.returncode == 0, result.stderr
    preview = tmp_path / "preview.json"
    page = tmp_path / "preview.html"
    preview.write_text(result.stdout)
    renderer = SCRIPT.parents[2] / "am-schedule-view" / "scripts" / "render-table.ps1"
    render = subprocess.run(["pwsh", "-NoProfile", "-File", renderer, "-Json", preview, "-Out", page],
                            capture_output=True, text=True)
    assert render.returncode == 0, render.stderr
    embedded = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                         page.read_text(encoding="utf-8-sig"), flags=re.S)
    assert embedded is not None
    payload = json.loads(embedded[1])
    assert payload["after"][0][0] == LARGE_ID and payload["after"][0][-1] == "Ventilation"
    assert payload["after"][1][-1] == "Keep this"


def test_preview_blocks_ineligible_and_inconsistent_rows(inputs):
    evidence, run = inputs
    evidence["elements"].append({"id": "23", "uid": "missing", "category_id": "-2008000",
                                 "state": "missing_parameter"})
    result = run()
    assert result.returncode == 1
    preview = json.loads(result.stdout)
    assert preview["blocked"] is True
    assert preview["warnings"] == [{"row": "23", "text": "The shared parameter is missing.", "blocking": True}]

    evidence["elements"][0]["value"] = "Do not overwrite this"
    result = run()
    assert result.returncode == 2 and not result.stdout


@pytest.mark.parametrize("override", [{"warnings": ["Unexpected native warning"]}, {"decision": "committed"}])
def test_preview_rejects_unverified_query_receipts(inputs, override):
    _, run = inputs
    result = run(**override)
    assert result.returncode == 2 and not result.stdout
    assert "clean query rollback" in result.stderr
