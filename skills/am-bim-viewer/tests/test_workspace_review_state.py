from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
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

        def interrupted(*_):
            raise OSError("synthetic write failure")

        monkeypatch.setattr(state.os, "replace", interrupted)
        with pytest.raises(OSError, match="synthetic write failure"):
            store.update("editor", TARGET, "operation-0000003", observed["etag"], {"status": "resolved"})
        assert store.read("viewer") == observed
        assert list(tmp_path.glob(".am-issues-*")) == []
