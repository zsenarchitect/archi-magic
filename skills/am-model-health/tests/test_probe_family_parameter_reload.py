from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import probe_family_parameter_reload as probe


@pytest.mark.parametrize("failure", [None, "late_shared", "second_callback", "late_inspection", "unexpected_effect", "inspection_event"])
def test_project_reload_rolls_back_the_entire_batch(monkeypatch, failure):
    status = SimpleNamespace(Uninitialized="initial", Started="started", Committed="committed",
                             RolledBack="rolled_back")
    events = []
    ids = ("20", "40")
    original = {key: [f"uid-{key}", "old"] for key in (*ids, "99")}
    elements = dict(original)
    families = {key: SimpleNamespace(Id=SimpleNamespace(Value=int(key)), UniqueId=f"uid-{key}",
                                    Name=f"Family {key}", FamilyCategory=SimpleNamespace(Id=SimpleNamespace(Value=-1)))
                for key in ids}
    names = {key: {"uid": family.UniqueId, "name": family.Name} for key, family in families.items()}
    bound = [(f"{key}.rfa", {"nested_families": [], "loaded_family": {"id": key, "uid": family.UniqueId},
                             "family": family.Name, "category_id": "-1"}) for key, family in families.items()]
    baseline = {"families": names, "targets": [{"loaded_family": item["loaded_family"],
                                                "project_values": {"types": []}} for _, item in bound]}

    class Subscription:
        def __iadd__(self, handler):
            self.handler = handler
            return self

        def __isub__(self, handler):
            assert handler is self.handler
            self.handler = None
            return self

    class Scope:
        def __init__(self, doc, name):
            self.name = name
            self.status = status.Uninitialized

        def Start(self):
            self.status = status.Started
            return self.status

        def GetStatus(self):
            return self.status

        def RollBack(self):
            events.append(("rollback", self.__class__.__name__))
            elements.clear()
            elements.update(original)
            self.status = status.RolledBack
            return self.status

        def Dispose(self):
            pass

    class Group(Scope):
        pass

    class Transaction(Scope):
        def Commit(self):
            self.status = status.Committed
            document.IsModifiable = False
            modified = [key for key in elements if original[key] != elements[key]]
            application.DocumentChanged.handler(None, SimpleNamespace(
                GetDocument=lambda: document, Operation="TransactionCommitted", GetTransactionNames=lambda: [self.name],
                GetAddedElementIds=lambda: [], GetDeletedElementIds=lambda: [],
                GetModifiedElementIds=lambda: [SimpleNamespace(Value=int(key)) for key in modified]))
            return self.status

        def Start(self):
            document.IsModifiable = True
            return super().Start()

        def RollBack(self):
            document.IsModifiable = False
            return super().RollBack()

    def load(path, options):
        assert document.IsModifiable
        key = Path(path).stem
        events.append(("load", key))
        if key == "40" and failure == "late_shared":
            assert options.OnSharedFamilyFound(families[key], True, None, None) == (False, "project", False)
            return False, None
        assert options.OnFamilyFound(True, None) == (True, False)
        if key == "40" and failure == "second_callback":
            assert options.OnFamilyFound(True, None) == (False, False)
        elements[key] = [families[key].UniqueId, "new"]
        return True, families[key]

    def inspect(ui, db, doc, family, target, sealed):
        assert not doc.IsModifiable
        key = str(family.Id.Value)
        events.append(("inspect", key))
        if key == "40" and failure == "late_inspection":
            raise ValueError("The second family state differs.")
        if key == "40" and failure == "inspection_event":
            application.DocumentChanged.handler(None, SimpleNamespace(GetDocument=lambda: document))
        return {"loaded_family": target["loaded_family"]}

    def regenerate():
        if failure == "unexpected_effect":
            elements["99"] = ["uid-99", "changed"]

    application = SimpleNamespace(DocumentChanged=Subscription())
    document = SimpleNamespace(Application=application, IsModifiable=False, Equals=lambda other: other is document,
                               LoadFamily=load, Regenerate=regenerate)
    db = SimpleNamespace(IFamilyLoadOptions=object, FamilySource=SimpleNamespace(Project="project"),
                         TransactionStatus=status, Transaction=Transaction, TransactionGroup=Group)
    monkeypatch.setattr(probe, "project_families", lambda *args: names)
    monkeypatch.setattr(probe, "inventory", lambda *args: dict(elements))
    monkeypatch.setattr(probe, "verify_loaded_family", inspect)

    result = probe.execute(None, db, document, baseline, bound, original)
    assert elements == original
    assert application.DocumentChanged.handler is None
    assert events[:2] == [("load", "20"), ("load", "40")]
    assert events[-1] == ("rollback", "Group")
    if failure:
        assert result["status"] == "stopped"
        if failure in {"late_shared", "second_callback"}:
            assert events[-2] == ("rollback", "Transaction")
        else:
            assert ("inspect", "20") in events
    else:
        assert result["status"] == "trial_rolled_back"
        assert result["effects"] == {"added": [], "deleted": [], "modified": ["20", "40"]}
        assert events[2:4] == [("inspect", "20"), ("inspect", "40")]


@pytest.fixture
def saved_reload_records():
    import json
    from family_library_discovery import manifest_digest
    from family_parameter_reload_preview import build_preview

    def sealed(body, digest_key):
        return dict(body, **{digest_key: manifest_digest(body)})

    def envelope(mode, identifier, decision, result, *, modified=(), approval=None, status="ok"):
        return {"mode": mode, "run_id": str(identifier), "status": status, "decision": decision, "approval": approval,
                "exit_code": 0, "result_truncated": False, "error": None, "engine": {"requested": "cpython"},
                "failures": [], "dialogs": [], "blocked": [], "warnings": [], "result": result,
                "changes": {"added": [], "deleted": [], "modified": [{"id": key} for key in modified],
                            "added_count": 0, "deleted_count": 0, "modified_count": len(modified),
                            "by_category": {"fixture": len(modified)} if modified else {},
                            "transactions": [result["transaction"]] if modified else []}}

    def audit(identifier, name=None, **extra):
        result = {"status": "ok", "audit_token": str(identifier) * 32, "trial_transaction": name, **extra}
        return envelope("query", identifier, "no_changes", result)

    project = {"project_uid": "fixture"}
    family = {"id": "20", "uid": "fixture-20"}
    target = sealed({"kind": "family-parameter-transfer-read", "version": 1, "role": "target",
                     "project": project, "loaded_family": family, "family": "Fixture", "category_id": "-1",
                     "revit_build": "fixture", "nested_families": [],
                     "state": {"coverage": {"complete": True, "issues": []}},
                     "project_values": {"types": [{"id": "21"}], "instances": []}}, "read_sha256")
    receipt = sealed({"kind": "family-transfer-derived-rfa", "version": 1, "rfa": {"bytes": 32},
                      "target_sha256": target["read_sha256"], "state_sha256": "a" * 64,
                      **{key: target[key] for key in ("project", "loaded_family", "family", "category_id",
                                                    "revit_build", "nested_families")}}, "derived_sha256")
    baseline = sealed({"kind": "family-transfer-reload-baseline", "version": 1, "project": project,
                       "revit_build": "fixture", "families": {"20": {"name": "Fixture", "uid": "fixture-20"}},
                       "inventory": {"sha256": "b" * 64, "element_count": 3}, "targets": [target]}, "baseline_sha256")
    preview = build_preview(baseline, [receipt])
    before = audit(1, snapshot_json=json.dumps(baseline))
    reload = {"baseline": before, "preview": preview, "disposable_fixture": True}
    transaction = probe.TRANSACTION_NAME + " " + "a" * 32
    result = {"status": "trial_rolled_back", "transaction": transaction, "rollback_verified": True,
              "preview_sha256": preview["preview_sha256"], "baseline_sha256": baseline["baseline_sha256"],
              "baseline_audit_token": before["result"]["audit_token"],
              "inventory": {"sha256": "c" * 64, "element_count": 3},
              "families": [{"loaded_family": family, "state_sha256": "a" * 64, "project_values_sha256": "d" * 64}],
              "effects": {"added": [], "deleted": [], "modified": ["20"]}}
    trial = envelope("dry_run", 2, "rolled_back", result, modified=("20",))
    restored = audit(3, transaction, snapshot_json=json.dumps(baseline))
    name = probe.TRANSACTION_NAME + " " + "b" * 32
    replayed = dict(result, status="awaiting_bridge_decision", transaction=name)
    kept = envelope("modify", 4, "committed", replayed, modified=("20",), approval="user")
    discarded = envelope("modify", 4, "discarded", replayed, modified=("20",), approval="user", status="rejected")
    fresh_keep = audit(5, name, project=project, preview_sha256=preview["preview_sha256"],
                       inventory=replayed["inventory"], families=replayed["families"])
    fresh_discard = audit(5, name, snapshot_json=json.dumps(baseline))
    return (reload, trial, restored), (kept, fresh_keep), (discarded, fresh_discard)


@pytest.mark.parametrize("keep", [True, False])
def test_reload_decision_requires_independent_project_readback(saved_reload_records, keep):
    from verify_family_parameter_reload import verify_decision

    trial, kept, discarded = saved_reload_records
    receipt = verify_decision(*trial, *(kept if keep else discarded))
    assert receipt["status"] == ("project_reload_kept" if keep else "project_reload_discarded")
    assert receipt["run_ids"] == ["1", "2", "3", "4", "5"]


@pytest.mark.parametrize("corruption", ["restored_scope", "reused_run", "unapproved", "changed_inventory", "changed_values"])
def test_reload_decision_blocks_incomplete_or_changed_evidence(saved_reload_records, corruption):
    from verify_family_parameter_reload import verify_decision

    trial, (modify, fresh), _ = saved_reload_records
    if corruption == "restored_scope":
        trial[2]["result"]["trial_transaction"] = None
    elif corruption == "reused_run":
        fresh["run_id"] = modify["run_id"]
    elif corruption == "unapproved":
        modify["approval"] = None
    elif corruption == "changed_inventory":
        fresh["result"]["inventory"] = dict(fresh["result"]["inventory"], sha256="e" * 64)
    elif corruption == "changed_values":
        row = dict(fresh["result"]["families"][0], project_values_sha256="e" * 64)
        fresh["result"]["families"] = [row]
    with pytest.raises(ValueError):
        verify_decision(*trial, modify, fresh)


def test_reload_handoff_rejects_changed_measurements_and_rechecks_ask(saved_reload_records, monkeypatch):
    import apply_family_parameter_reload as replay

    (reload, trial, after), _, _ = saved_reload_records
    checks = []

    def run_trial(ui, db, document, requested, *, handoff):
        assert requested is reload
        with pytest.raises(ValueError, match="differs"):
            handoff([], trial["result"]["effects"])
        handoff(trial["result"]["families"], trial["result"]["effects"])
        return "verified"

    monkeypatch.setattr(replay, "require_ask", lambda context: checks.append(context))
    monkeypatch.setattr(replay, "run_trial", run_trial)
    result = replay.apply(None, None, None, {"reload": reload, "trial": trial, "after": after}, "ask")
    assert result == "verified"
    assert checks == ["ask", "ask"]
