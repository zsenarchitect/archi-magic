from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workspace_review_state as state


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Office state locking requires POSIX.")
MEMBERS = {"editor": "editor", "viewer": "viewer"}
TARGET = "board:coordination:12"
OPERATION = "operation-0000001"


def test_workspace_review_survives_restart_and_replays_exact_write(tmp_path):
    with state.ReviewState(tmp_path, "sample", MEMBERS, [TARGET]) as store:
        initial = store.read("viewer")["etag"]
        patch = {"comment": "Check the opening.", "status": "waived", "assignee": "viewer"}
        result = store.update("editor", TARGET, OPERATION, initial, patch)
        with pytest.raises(state.RevisionConflict):
            store.update("editor", TARGET, "operation-0000002", initial, {"status": "open"})
        with pytest.raises(PermissionError):
            store.update("viewer", TARGET, OPERATION, initial, patch)
        with pytest.raises(BlockingIOError):
            with state.ReviewState(tmp_path, "sample", MEMBERS, [TARGET]):
                pytest.fail("A second service acquired the project lock.")

    with state.ReviewState(tmp_path, "sample", MEMBERS, []) as restarted:
        assert restarted.update("editor", TARGET, OPERATION, initial, patch) == result
        observed = restarted.read("viewer")
        assert observed["etag"] == result["etag"]
        item = observed["annotations"][TARGET]
        assert (item["status"], item["assignee"]) == ("waived", "viewer")
        assert [(note["actor"], note["text"]) for note in item["comments"]] == [("editor", "Check the opening.")]
        with pytest.raises(state.OperationConflict):
            restarted.update("editor", TARGET, OPERATION, initial, {"status": "open"})
        with pytest.raises(ValueError, match="retired"):
            restarted.update("editor", TARGET, "operation-0000003", observed["etag"], {"status": "open"})


def test_workspace_review_keeps_exact_revision_across_conflict_and_failed_replace(tmp_path, monkeypatch):
    with state.ReviewState(tmp_path, "sample", MEMBERS, [TARGET]) as store:
        initial = store.read("editor")["etag"]

        def change(number):
            try:
                return store.update("editor", TARGET, f"operation-{number:07d}", initial,
                                    {"comment": f"Comment {number}"})
            except state.RevisionConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(change, (1, 2)))
        assert sum(result is not None for result in results) == 1
        observed = store.read("viewer")
        assert len(observed["annotations"][TARGET]["comments"]) == 1

        def interrupted(*_, **__):
            raise OSError("synthetic write failure")

        monkeypatch.setattr(state.os, "replace", interrupted)
        with pytest.raises(OSError, match="synthetic write failure"):
            store.update("editor", TARGET, "operation-0000003", observed["etag"], {"status": "resolved"})
        assert store.read("viewer") == observed
        assert list(tmp_path.glob(".am-issues-*")) == []


def test_workspace_maintenance_restores_history_and_replay_without_overwriting(tmp_path):
    root, restored = tmp_path / "original", tmp_path / "restored"
    root.mkdir()
    restored.mkdir()
    snapshot = tmp_path / "private-review.json"
    cli = Path(state.__file__).with_name("workspace_maintenance.py")

    def maintenance(action, project, folder, file=snapshot):
        return subprocess.run([sys.executable, str(cli), action, project, str(folder.resolve()), str(file)],
                              capture_output=True, text=True)

    with state.ReviewState(root, "sample", MEMBERS, [TARGET]) as store:
        initial = store.read("editor")["etag"]
        patch = {"status": "resolved", "assignee": "viewer", "comment": "Confirm before issue."}
        committed = store.update("editor", TARGET, OPERATION, initial, patch)
        artifact = {"kind": "audit", "generation": 5, "digest": "a" * 64, "data": {}}
        tag = store.refresh_artifacts({"latest": {}}, lambda *_: (artifact, []))
        observed = store.read("viewer")
        busy = maintenance("export", "sample", root)
        assert busy.returncode == 2 and "lock is busy" in busy.stderr
        assert not snapshot.exists()

    result = maintenance("export", "sample", root)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"action": "export", "project": "sample", "etag": tag}
    raw = snapshot.read_bytes()
    assert raw == (root / ".am-issues.json").read_bytes()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    duplicate = maintenance("export", "sample", root)
    assert duplicate.returncode == 2 and "already exists" in duplicate.stderr
    assert snapshot.read_bytes() == raw

    assert maintenance("import", "other", restored).returncode == 2
    assert not (restored / ".am-issues.json").exists()
    corrupt = tmp_path / "corrupt.json"
    bad = json.loads(raw)
    bad["operations"]["editor"][OPERATION]["result"]["etag"] = '"sample-100"'
    corrupt.write_text(json.dumps(bad))
    assert maintenance("import", "sample", restored, corrupt).returncode == 2
    corrupt.write_bytes(raw.replace(b'"version":1', b'"version":1,"version":1'))
    assert maintenance("import", "sample", restored, corrupt).returncode == 2
    assert not (restored / ".am-issues.json").exists()

    result = maintenance("import", "sample", restored)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["etag"] == tag
    assert (restored / ".am-issues.json").read_bytes() == raw
    with state.ReviewState(restored, "sample", MEMBERS, []) as restarted:
        assert restarted.read("viewer") == observed
        assert restarted.update("editor", TARGET, OPERATION, initial, patch) == committed
        old = dict(artifact, generation=4)
        assert restarted.refresh_artifacts({"latest": {}}, lambda *_: (old, [])) is None
        assert restarted.read_artifact("viewer", "latest") == {"freshness": "unavailable"}

    existing = maintenance("import", "sample", restored)
    assert existing.returncode == 2 and "already exists" in existing.stderr
    assert (restored / ".am-issues.json").read_bytes() == raw
