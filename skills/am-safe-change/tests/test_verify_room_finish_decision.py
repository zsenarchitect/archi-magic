from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_room_finish_decision.py"
sys.path.insert(0, str(SCRIPT.parent))
from verify_room_finish_decision import TRANSACTION_NAME, verify


def record(run_id, mode, decision, status, result, added=(), approval=None):
    rows = [{"id": identifier, "class": kind} for identifier, kind in added]
    changes = {
        "added_count": len(rows), "added": rows,
        "modified_count": 0, "modified": [], "deleted_count": 0, "deleted": [],
        "by_category": {"Fixtures": len(rows)} if rows else {},
        "transactions": [TRANSACTION_NAME] if rows else [],
    }
    return {"run_id": run_id, "mode": mode, "decision": decision, "status": status,
            "result": result, "changes": changes, "approval": approval, "exit_code": 0,
            "result_truncated": False}


@pytest.fixture
def fixture():
    basis = "b" * 64
    plan = {"kind": "change-preview", "recipe": "room-finishes", "blocked": False, "basis_sha256": basis,
            "columns": ["Piece", "Room ID", "Finish"]}
    plan["steps"] = [{"piece_key": key * 64, "room_id": "1"} for key in "123"]
    plan["after"] = [[step["piece_key"], step["room_id"], kind] for step, kind in
                     zip(plan["steps"], ("floor", "ceiling", "wall"))]

    def placement(start):
        created = [{"id": str(start + index), "uid": "fixture-%d" % (start + index),
                    "piece_key": step["piece_key"], "observed_sha256": "a" * 64}
                   for index, step in enumerate(plan["steps"])]
        sketches = [{"id": str(start + index), "uid": "fixture-%d" % (start + index)} for index in (3, 4)]
        receipt = {"status": "ok", "basis_sha256": basis, "created": created,
                   "added_sketch_elements": sketches, "added_sketch_element_ids": [row["id"] for row in sketches]}
        classes = ("Floor", "Ceiling", "Wall", "Sketch", "ModelLine")
        effects = [(int(item["id"]), kind) for item, kind in zip(created + sketches, classes)]
        absent = {"status": "rollback_verified", "basis_sha256": basis, "confirmed_absent": [
            {key: item[key] for key in ("id", "uid")} for item in created + sketches
        ]}
        return receipt, effects, absent

    dry, dry_effects, dry_absent = placement(100)
    replay, effects, absent = placement(200)
    inputs = SimpleNamespace(
        plan=plan, dry=record("dry", "dry_run", "rolled_back", "ok", dry, dry_effects),
        dry_query=record("dry-query", "query", "rolled_back", "ok", dry_absent),
        final=record("modify", "modify", "committed", "ok", replay, effects, "user"),
        fresh=record("fresh", "query", "rolled_back", "ok", {
            "status": "keep_verified", "basis_sha256": basis, "created": deepcopy(replay["created"]),
            "added_sketch_elements": deepcopy(replay["added_sketch_elements"]), "repeat_creates": 0,
        }),
        absent=absent,
    )
    inputs.verify = lambda: verify(inputs.plan, inputs.dry, inputs.dry_query, inputs.final, inputs.fresh)
    return inputs


def test_keep_and_discard_are_verified_from_separate_runs(fixture):
    assert fixture.verify()["status"] == "verified_room_finish_committed"
    fixture.final.update(status="rejected", decision="discarded")
    fixture.fresh["result"] = fixture.absent
    assert fixture.verify()["status"] == "verified_room_finish_discarded"


@pytest.mark.parametrize(("change", "reason"), [
    (lambda x: x.final.update(approval="auto"), "reviewed Revit outcome"),
    (lambda x: x.dry_query["result"]["confirmed_absent"].pop(), "exact additions were restored"),
    (lambda x: x.final["result"]["created"][0].update(observed_sha256="c" * 64), "verified dry run"),
    (lambda x: x.final["changes"]["added"][0].update(id=999), "exact placement receipt"),
    (lambda x: x.final["changes"]["added"].pop(), "every added finish"),
    (lambda x: x.final["changes"].update(transactions=["Other"]), "reviewed finish transaction"),
    (lambda x: x.fresh.update(run_id="dry-query"), "separate pyRevit runs"),
    (lambda x: x.fresh["result"].update(repeat_creates=1), "repeat behavior"),
])
def test_evidence_mismatches_stop_verification(fixture, change, reason):
    change(fixture)
    with pytest.raises(ValueError, match=reason):
        fixture.verify()


def test_cli_verifies_saved_py_revit_responses(tmp_path, fixture):
    command = [sys.executable, str(SCRIPT)]
    for name, value in (("plan", fixture.plan), ("dry-run", fixture.dry), ("dry-readback", fixture.dry_query),
                        ("decision", fixture.final), ("readback", fixture.fresh)):
        path = tmp_path / (name + ".json")
        path.write_text(json.dumps(value if name == "plan" else {"response": value}), encoding="utf-8")
        command.extend(["--" + name, str(path)])
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["finish_count"] == 3
