import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import workspace_artifacts as artifacts


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Office publication requires POSIX directory descriptors.")
AUDIT = {"findings": [{"severity": "warning", "message": "Review the opening."}]}
SLOT = {"kind": "audit", "path": "audit.json"}


def test_artifact_cli_publishes_completed_audit_once_and_rejects_an_older_run(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"projects": {"sample": {"root": str(project.resolve()), "artifacts": {"latest": SLOT}}}}))
    source = tmp_path / "saved-audit.json"
    source.write_text(json.dumps({"status": "ok", "mode": "query", "decision": "rolled_back", "result": AUDIT}))
    command = [sys.executable, str(SCRIPTS / "workspace_artifacts.py"), str(config), "sample", "latest"]
    published = subprocess.run([*command, "3", str(source)], capture_output=True, text=True)
    assert published.returncode == 0, published.stderr
    receipt = json.loads(published.stdout)
    assert receipt["status"] == "published"
    assert artifacts.read_published(project.resolve(), SLOT) == {
        "kind": "audit", "board": None, "data": AUDIT, "generation": 3, "digest": receipt["digest"],
    }
    duplicate = subprocess.run([*command, "3", str(source)], capture_output=True, text=True)
    assert duplicate.returncode == 0 and json.loads(duplicate.stdout)["status"] == "already_published"
    delayed = subprocess.run([*command, "2", str(source)], capture_output=True, text=True)
    assert delayed.returncode == 2
    assert artifacts.read_published(project.resolve(), SLOT)["generation"] == 3
    with pytest.raises(ValueError, match="different content"):
        artifacts.publish(project.resolve(), SLOT, 3, {"findings": []})


def test_board_publication_requires_exact_note_and_rejects_duplicate_issue_numbers(tmp_path):
    identity = {"project_uid": "project-uid", "path_sha256": "a" * 64}
    slot = {"kind": "board", "path": "board.json",
            "board": {"lineage": "coordination", "host_identity": identity, "note_uid": "note-uid"}}
    text = "Board\rIssues use fixed numbers; next new issue is #2. Never renumber.\r#1 [OPEN] Check the opening"

    def record(value, note_uid="note-uid", host=identity):
        return {"host_identity": host, "note": {"unique_id": note_uid, "text": value,
                                                "sha1": hashlib.sha1(value.encode()).hexdigest()}}

    root = tmp_path.resolve()
    with pytest.raises(ValueError, match="configured Revit model and note"):
        artifacts.publish(root, slot, 1, record(text, note_uid="replacement"))
    with pytest.raises(ValueError, match="configured Revit model and note"):
        artifacts.publish(root, slot, 1, record(text, host=dict(identity, project_uid="other")))
    with pytest.raises(ValueError, match="duplicate issue numbers"):
        artifacts.publish(root, slot, 1, record(text + "\r#1 [DONE] Duplicate"))
    artifacts.publish(root, slot, 1, record(text))
    board = artifacts.read_published(root, slot)
    assert board["board"]["lineage"] == "coordination"
    assert [(row["number"], row["title"]) for row in board["data"]["issues"]] == [(1, "Check the opening")]
    artifacts.publish(root, slot, 2, record(text.replace("[OPEN]", "[DONE]")))
    assert artifacts.read_published(root, slot)["data"]["issues"][0]["status"] == "DONE"


def test_artifact_slot_rejects_links_and_preserves_last_record_after_failed_replace(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    artifacts.publish(root, SLOT, 1, AUDIT)
    outside = root / "outside"
    outside.mkdir()
    (root / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        artifacts.publish(root, dict(SLOT, path="alias/audit.json"), 2, AUDIT)
    with pytest.raises(ValueError, match="non-hidden"):
        artifacts.publish(root, dict(SLOT, path=".am-issues.json"), 2, AUDIT)
    assert list(outside.iterdir()) == []

    def interrupted(*_, **__):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(artifacts.os, "replace", interrupted)
    with pytest.raises(OSError, match="synthetic publication failure"):
        artifacts.publish(root, SLOT, 2, {"findings": []})
    assert artifacts.read_published(root, SLOT)["generation"] == 1
    assert list(root.glob(".am-publish-*")) == []


def test_published_preview_excludes_model_execution_fields(tmp_path):
    preview = {"kind": "change-preview", "columns": ["Element ID", "Text"], "keyColumn": "Element ID",
               "before": [["9007199254740993", "Old"]], "after": [["9007199254740993", "New"]],
               "changedColumns": ["Text"], "steps": [{"private": "action"}], "review_only": True}
    slot = {"kind": "preview", "path": "preview.json"}
    artifacts.publish(tmp_path.resolve(), slot, 1, {"preview": preview, "plan": {"private": "plan"}})
    data = artifacts.read_published(tmp_path.resolve(), slot)["data"]
    assert data == {key: value for key, value in preview.items() if key != "steps"}


def test_document_page_accepts_largest_table_and_comparison_records():
    from workspace_documents import encode_page

    text = "🧭" * 3998 + '\n"'
    columns = [str(index) + text[len(str(index)):] for index in range(128)]
    source = {"file": "Rooms.xlsx", "format": "xlsx", "header_row": 1, "sheet": "Room Data"}
    slot = {"kind": "document", "path": "document.json"}
    table = {"source": source, "columns": columns, "rows": [{"source_row": index, "cells": [text] * 128} for index in (2, 3)]}
    comparison = {"status": "differences", "document": source,
                  "schedule": {"file": "schedule.json", "schedule": "Room Schedule"},
                  "counts": {"expected_rows": 1, "schedule_rows": 0, "mismatched_cells": 0,
                             "missing_in_schedule": 1, "actual_only_rows": 0},
                  "mismatches": [], "actual_only": [],
                  "missing_in_schedule": [{"key": dict.fromkeys(columns, text), "source_row": 2}]}
    for raw, collection in ((table, "rows"), (comparison, "missing_in_schedule")):
        record = artifacts.make_artifact(slot, 1, raw)
        encoded = encode_page(record["data"], collection, 0, 1, record["digest"], "ready")
        assert len(encoded) < 16 * 1024 * 1024
        page = json.loads(encoded)
        if collection == "rows":
            assert page["rows"] == raw[collection][:1] and page["next_index"] == 1
            next_page = json.loads(encode_page(record["data"], collection, 1, 1, record["digest"], "ready"))
            assert next_page["rows"] == raw[collection][1:] and next_page["next_index"] is None
        else:
            assert page["rows"] == raw[collection] and page["next_index"] is None
