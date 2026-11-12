from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import probe_mep_penetration_placement as probe
from mep_penetration_preview import preview
from test_mep_penetration_preview import evidence


def test_placement_preview_binds_exact_symbol_and_ownership_ids(evidence):
    key = preview(evidence)["findings"][0]["crossing_key"]
    evidence["symbols"] = [{"id": "30", "uid": "opening-type", "version_guid": "version",
                            "is_active": False, "placement_type": "WorkPlaneBased"}]
    choices = {"crossing_keys": [key], "symbols": {"wall:rectangular": {
        "id": "30", "uid": "opening-type", "parameters": {"width": "101", "height": "102", "depth": "103"}}}}
    plan = probe.placement_plan(evidence, choices)
    assert plan["ownership_ids"] == ["10", "30"]
    assert plan["rows"][0]["symbol"]["is_active"] is False
    assert plan["rows"][0]["crossing_key"] == key
    choices["symbols"]["wall:rectangular"]["uid"] = "another-symbol"
    with pytest.raises(ValueError, match="native evidence"):
        probe.placement_plan(evidence, choices)


def test_cut_verification_requires_unchanged_existing_cuts(evidence):
    row = preview(evidence)["findings"][0]
    geometry = row["geometry"]
    before = evidence["hosts"][0]
    old = {"key": "cut-1", "shape": "round", "center": [6, 3], "diameter": 1}
    before["existing"] = [old]
    new = dict(geometry["outline"], key="cut-2")
    current = dict(before, existing=[old, new])
    assert probe.verify_new_cut(before, current, geometry, 0.001) == (new, pytest.approx(3.75))
    current["existing"] = [dict(old, diameter=2), new]
    with pytest.raises(ValueError, match="prior opening changed"):
        probe.verify_new_cut(before, current, geometry, 0.001)
    current["existing"] = [old, dict(new, width=2)]
    with pytest.raises(ValueError, match="actual through-cut"):
        probe.verify_new_cut(before, current, geometry, 0.001)


@pytest.mark.parametrize("failure", [None, "late_tracking", "unreviewed_effect", "commit_drift", "handoff"])
def test_trial_rolls_back_openings_cuts_and_tags_as_one_batch(monkeypatch, failure):
    statuses = SimpleNamespace(Uninitialized="uninitialized", Started="started", Committed="committed",
                               RolledBack="rolled_back")
    original = {"10": ("wall-10", "before"), "11": ("wall-11", "before"), "99": ("other-99", "before")}
    contents = dict(original)
    tags = set()
    actions = []

    class Subscription:
        def __iadd__(self, handler):
            self.handler = handler
            return self

        def __isub__(self, handler):
            assert handler is self.handler
            self.handler = None
            return self

    class Scope:
        def __init__(self, document, name):
            self.name = name
            self.state = statuses.Uninitialized

        def Start(self):
            self.state = statuses.Started
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            actions.append(("rollback", self.__class__.__name__))
            contents.clear()
            contents.update(original)
            tags.clear()
            self.state = statuses.RolledBack
            return self.state

        def Dispose(self):
            pass

    class Transaction(Scope):
        def Commit(self):
            self.state = statuses.Committed
            changed = ["10", "11"] + (["99"] if failure == "unreviewed_effect" else [])
            app.DocumentChanged.handler(None, SimpleNamespace(
                GetDocument=lambda: doc, Operation="TransactionCommitted", GetTransactionNames=lambda: [self.name],
                GetAddedElementIds=lambda: [SimpleNamespace(Value=index) for index in (30, 31)],
                GetDeletedElementIds=lambda: [], GetModifiedElementIds=lambda: [
                    SimpleNamespace(Value=int(key)) for key in changed]))
            return self.state

    class Group(Scope):
        def Assimilate(self):
            actions.append(("handoff", self.__class__.__name__))
            self.state = statuses.Committed
            return self.state

    def place(document, DB, snapshot, row, budget):
        host_id = row["host_id"]
        identifier = str(int(host_id) + 20)
        name = f"opening-{identifier}"
        contents[host_id] = (original[host_id][0], "cut")
        contents[identifier] = (name, "new")
        actions.append(("cut", host_id))
        if host_id == "11" and failure == "late_tracking":
            raise ValueError("The second opening tracking record failed.")
        tags.add(name)
        return {"opening_id": identifier, "opening_uid": name, "activated_symbol": None}

    app = SimpleNamespace(DocumentChanged=Subscription())
    doc = SimpleNamespace(Application=app, Equals=lambda other: other is doc,
                          GetElement=lambda unique_id: next((row for row in contents.values() if row[0] == unique_id), None))
    db = SimpleNamespace(TransactionStatus=statuses, TransactionGroup=Group, Transaction=Transaction)
    def verify(*args):
        if failure == "commit_drift":
            raise ValueError("The provisional readback changed.")

    monkeypatch.setattr(probe, "verify_after_commit", verify)
    monkeypatch.setattr(probe, "place_one", place)
    monkeypatch.setattr(probe, "project_inventory", lambda *args: dict(contents))
    plan = {"rows": [{"host_id": "10"}, {"host_id": "11"}]}
    snapshot = {"baseline": True}
    def approve(rows, effects):
        assert set(effects["added"]) == {"30", "31"}

    result = probe.execute_trial(doc, db, snapshot, plan, lambda: snapshot,
                                 handoff=approve if failure == "handoff" else None)
    if failure == "handoff":
        assert result["status"] == "awaiting_bridge_decision"
        assert tags == {"opening-30", "opening-31"}
        assert actions[-1] == ("handoff", "Group")
        return
    assert contents == original
    assert not tags
    assert actions[:2] == [("cut", "10"), ("cut", "11")]
    assert actions[-1] == ("rollback", "Group")
    assert result["rollback_verified"]
    if failure:
        assert result["status"] == "stopped"
    else:
        assert result["status"] == "trial_rolled_back"
        assert result["effects"]["added"] == ["30", "31"]


@pytest.fixture
def saved_trial(evidence):
    import json
    from mep_penetration_preview import fingerprint

    key = preview(evidence)["findings"][0]["crossing_key"]
    evidence["symbols"] = [{"id": "30", "uid": "opening-type", "version_guid": "version",
                            "is_active": False, "placement_type": "WorkPlaneBased"}]
    choices = {"crossing_keys": [key], "symbols": {"wall:rectangular": {
        "id": "30", "uid": "opening-type", "parameters": {"width": "101", "height": "102", "depth": "103"}}}}
    plan = probe.placement_plan(evidence, choices)
    name = probe.TRANSACTION_NAME + " " + "e" * 32
    inventory = "a" * 64

    def envelope(mode, identifier, decision, result, effects=None):
        effects = effects or {"added": [], "deleted": [], "modified": []}
        return {"mode": mode, "run_id": identifier, "status": "ok", "decision": decision,
                "exit_code": 0, "result_truncated": False, "engine": {"requested": "cpython"}, "result": result,
                "changes": {**{key + "_count": len(values) for key, values in effects.items()},
                            **{key: [{"id": value} for value in values] for key, values in effects.items()},
                            "transactions": [name] if any(effects.values()) else []}}

    before = envelope("query", "before", "no_changes", {
        "status": "ok", "audit_token": "1" * 32, "snapshot_json": json.dumps(evidence),
        "inventory_sha256": inventory, "trial_transaction": None, "absent_opening_uids": []})
    effects = {"added": ["40"], "deleted": [], "modified": ["10", "30"]}
    result = {"status": "trial_rolled_back", "rollback_verified": True, "preview_sha256": fingerprint(plan),
              "baseline_audit_token": "1" * 32, "baseline_inventory_sha256": inventory, "transaction": name,
              "effects": effects, "rows": [
                  {"crossing_key": key, "opening_id": "40", "opening_uid": "opening-40", "activated_symbol": "30",
                   "opening_sha256": "b" * 64, "tracking_sha256": "c" * 64, "removed_volume_ft3": 3.75,
                   "cut_outline": dict(plan["rows"][0]["geometry"]["outline"])}]}
    trial = envelope("dry_run", "trial", "rolled_back", result, effects=effects)
    after = envelope("query", "after", "no_changes", {
        "status": "ok", "audit_token": "2" * 32, "snapshot_json": json.dumps(evidence),
        "inventory_sha256": inventory, "trial_transaction": name, "absent_opening_uids": ["opening-40"]})
    return choices, before, trial, after


def test_trial_verifier_reads_complete_saved_records_through_cli(saved_trial, tmp_path):
    import json
    import subprocess

    paths = [tmp_path / f"record-{index}.json" for index in range(4)]
    for path, record in zip(paths, saved_trial):
        path.write_text(json.dumps(record))
    run = subprocess.run([sys.executable, str(SCRIPTS / "verify_mep_penetration_trial.py"),
                          *map(str, paths)], capture_output=True, text=True, check=True)
    result = json.loads(run.stdout)
    assert result["status"] == "mep_opening_trial_verified"
    assert result["run_ids"] == ["before", "trial", "after"]
    assert result["effects"] == {"added": ["40"], "deleted": [], "modified": ["10", "30"]}


@pytest.mark.parametrize("problem", [
    "reused_run", "stale_inventory", "missing_absence", "wrong_cut", "wrong_volume", "wrong_scope", "truncated",
])
def test_trial_verifier_blocks_changed_or_partial_evidence(saved_trial, problem):
    from verify_mep_penetration_trial import verify

    choices, before, trial, after = saved_trial
    if problem == "reused_run":
        after["run_id"] = before["run_id"]
    elif problem == "stale_inventory":
        after["result"]["inventory_sha256"] = "f" * 64
    elif problem == "missing_absence":
        after["result"]["absent_opening_uids"] = []
    elif problem == "wrong_cut":
        trial["result"]["rows"][0]["cut_outline"]["height"] = 2.0
    elif problem == "wrong_volume":
        trial["result"]["rows"][0]["removed_volume_ft3"] = 3
    elif problem == "wrong_scope":
        trial["changes"]["modified"][0]["id"] = "99"
    elif problem == "truncated":
        trial["result_truncated"] = True
    with pytest.raises(ValueError):
        verify(choices, before, trial, after)


def test_trial_verifier_accepts_bridge_no_changes_after_internal_rollback(saved_trial):
    from verify_mep_penetration_trial import verify

    choices, before, trial, after = saved_trial
    trial["decision"] = "no_changes"
    trial["changes"] = before["changes"]
    assert verify(choices, before, trial, after)["status"] == "mep_opening_trial_verified"


@pytest.fixture
def saved_decision(saved_trial):
    from copy import deepcopy
    import json
    from mep_penetration_preview import fingerprint

    choices, before, dry, after = saved_trial
    name = probe.TRANSACTION_NAME + " " + "f" * 32
    replay = deepcopy(dry["result"])
    replay.update(status="awaiting_bridge_decision", transaction=name, inventory_sha256="e" * 64,
                  runtime_sha256="f" * 64)
    row = replay["rows"][0]
    row.update(opening_id="55", opening_uid="opening-55", tracking_sha256="9" * 64)
    replay["effects"]["added"] = ["55"]
    modify = dict(dry, run_id="replay", mode="modify", decision="committed", approval="user", result=replay)
    modify["changes"] = {**dry["changes"], "added": [{"id": "55"}], "transactions": [name]}
    discard = dict(modify, decision="discarded", status="rejected")
    kept_snapshot = json.loads(before["result"]["snapshot_json"])
    geometry = probe.placement_plan(kept_snapshot, choices)["rows"][0]["geometry"]
    kept_snapshot["pairs"][0].update(status="gross_crossing", existing_cut_verified=True, gross_verified=True)
    kept_snapshot["tracked"] = [{"crossing_key": row["crossing_key"], "host_uid": "wall-10", "run_uid": "duct-20",
                                "opening_uid": "opening-55", "observed_uid": "opening-55", "observed_id": "55",
                                "observed_host_uid": "wall-10", "cut_verified": True,
                                "opening_sha256": row["opening_sha256"], "observed_opening_sha256": row["opening_sha256"],
                                "tracking_sha256": row["tracking_sha256"], "geometry": geometry,
                                "geometry_sha256": fingerprint(geometry)}]
    keep_audit = dict(after, run_id="fresh", result={
        **after["result"], "audit_token": "3" * 32, "inventory_sha256": replay["inventory_sha256"],
        "trial_transaction": name, "kept": True, "absent_opening_uids": [], "snapshot_json": json.dumps(kept_snapshot)})
    discard_audit = dict(after, run_id="fresh", result={
        **after["result"], "audit_token": "3" * 32, "trial_transaction": name, "absent_opening_uids": ["opening-55"]})
    return saved_trial, (modify, keep_audit), (discard, discard_audit)


@pytest.mark.parametrize("keep", [True, False])
def test_decision_audits_actual_replay_ids_instead_of_trial_ids(saved_decision, keep):
    from verify_mep_penetration_decision import verify_decision

    base, kept, discarded = saved_decision
    receipt = verify_decision(*base, *(kept if keep else discarded))
    assert receipt["status"] == ("mep_openings_kept" if keep else "mep_openings_discarded")
    assert receipt["opening_ids"] == (["55"] if keep else [])


@pytest.mark.parametrize("problem", ["no_approval", "reused_audit", "wrong_opening", "wrong_inventory", "wrong_effect"])
def test_decision_rejects_wrong_native_outcome_or_readback(saved_decision, problem):
    import json
    from verify_mep_penetration_decision import verify_decision

    base, (modify, fresh), _ = saved_decision
    if problem == "no_approval":
        modify["approval"] = "auto"
    elif problem == "reused_audit":
        fresh["result"]["audit_token"] = base[-1]["result"]["audit_token"]
    elif problem == "wrong_opening":
        snapshot = json.loads(fresh["result"]["snapshot_json"])
        snapshot["tracked"][0]["observed_id"] = "40"
        fresh["result"]["snapshot_json"] = json.dumps(snapshot)
    elif problem == "wrong_inventory":
        fresh["result"]["inventory_sha256"] = "0" * 64
    elif problem == "wrong_effect":
        modify["result"]["effects"]["modified"] = ["10"]
        modify["changes"]["modified"] = [{"id": "10"}]
        modify["changes"]["modified_count"] = 1
    with pytest.raises(ValueError):
        verify_decision(*base, modify, fresh)


def test_apply_requires_exact_native_ask_controls_and_rechecks_before_handoff(saved_decision, monkeypatch):
    import apply_mep_penetration_placement as application

    (choices, before, dry, after), (modify, _), _ = saved_decision
    expected = "f" * 64
    observed = []
    control = {"runtime_control": "disposable_ask", "runtime_sha256": expected,
               "project": {"project_uid": "fixture", "path_sha256": "a" * 64},
               "control_id": "90", "before": application.CONTROL_BEFORE, "after": application.CONTROL_AFTER}
    changes = {"added_count": 0, "deleted_count": 0, "modified_count": 1, "modified": [{"id": "90"}]}
    keep = dict(modify, run_id="control-keep", changes=changes, result=control)
    discard = dict(keep, run_id="control-discard", status="rejected", decision="discarded")
    inputs = {"runtime_controls": {"keep": keep, "discard": discard}, "placement": {
        "choices": choices, "preview_sha256": dry["result"]["preview_sha256"],
        "baseline_inventory_sha256": dry["result"]["baseline_inventory_sha256"],
        "baseline_audit_token": dry["result"]["baseline_audit_token"]},
        "before": before, "trial": dry, "after": after}

    def read_policy(context):
        observed.append(context)
        return expected

    def trial(document, DB, replay, *, handoff):
        assert replay == inputs["placement"]
        handoff(modify["result"]["rows"], modify["result"]["effects"])
        return modify["result"]

    monkeypatch.setattr(application, "runtime_identity", read_policy)
    monkeypatch.setattr(application, "trial", trial)
    context = SimpleNamespace(Mode="modify")
    assert application.apply(None, None, inputs, context)["runtime_sha256"] == expected
    assert observed == [context, context]
    discard["approval"] = "auto"
    with pytest.raises(ValueError):
        application.apply(None, None, inputs, context)
    assert observed == [context, context]
