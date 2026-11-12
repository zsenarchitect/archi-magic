from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
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
    assert probe.verify_new_cut(before, current, geometry, 0.001) == pytest.approx(3.75)
    current["existing"] = [dict(old, diameter=2), new]
    with pytest.raises(ValueError, match="prior opening changed"):
        probe.verify_new_cut(before, current, geometry, 0.001)
    current["existing"] = [old, dict(new, width=2)]
    with pytest.raises(ValueError, match="actual through-cut"):
        probe.verify_new_cut(before, current, geometry, 0.001)


@pytest.mark.parametrize("failure", [None, "late_tracking", "unreviewed_effect", "commit_drift"])
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
        pass

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
    result = probe.execute_trial(doc, db, snapshot, plan, lambda: snapshot)
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
