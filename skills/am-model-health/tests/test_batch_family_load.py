import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import probe_batch_family_load as probe


@pytest.mark.parametrize("failure", [None, "second_type", "extra_effect"])
def test_exact_multi_type_load_restores_baseline_after_trial(monkeypatch, failure):
    def identifier(value):
        return SimpleNamespace(Value=value)

    class EventHook(list):
        def __iadd__(self, handler):
            self.append(handler)
            return self

        def __isub__(self, handler):
            self.remove(handler)
            return self

    class Scope:
        def __init__(self, document, name):
            self.name = name
            self.state = "Uninitialized"

        def Start(self):
            self.state = "Started"
            return self.state

        def GetStatus(self):
            return self.state

        def Assimilate(self):
            self.state = "Committed"
            return self.state

        def RollBack(self):
            symbols.clear()
            self.state = "RolledBack"
            return self.state

        def Dispose(self):
            pass

    class Transaction(Scope):
        def Commit(self):
            self.state = "Committed"
            event = SimpleNamespace(
                Operation="TransactionCommitted", GetDocument=lambda: project,
                GetTransactionNames=lambda: [self.name],
                GetAddedElementIds=lambda: [identifier(value) for value in (40, 41, 42)],
                GetModifiedElementIds=lambda: [identifier(19 if failure == "extra_effect" else 40)],
                GetDeletedElementIds=lambda: [],
            )
            for listener in project.Application.DocumentChanged:
                listener(None, event)
            return self.state

    symbols = {}
    attempts = []
    family = SimpleNamespace(Id=identifier(40), UniqueId="family-40", Name="Fixture",
                             FamilyCategory=SimpleNamespace(Id=identifier(-1000)),
                             GetFamilySymbolIds=lambda: [symbol.Id for symbol in symbols.values()])

    def load(path, name, options):
        assert path == "sealed/Fixture.rfa"
        attempts.append(name)
        if failure == "second_type" and name == "Wide":
            assert options.OnFamilyFound(True, True) == (False, False)
            return False, None
        key = 41 + len(symbols)
        symbol = SimpleNamespace(Id=identifier(key), UniqueId=f"symbol-{key}", Name=name, Family=family)
        symbols[name] = symbol
        return True, symbol

    def snapshot(*args, **kwargs):
        rows = []
        for line, name in [(2, "Narrow"), (3, "Wide")]:
            symbol = symbols.get(name)
            existing = ({"family_id": "40", "family_uid": "family-40",
                         "symbol_id": str(symbol.Id.Value), "symbol_uid": symbol.UniqueId} if symbol else None)
            rows.append({"line": line, "existing": existing, "issues": [], "differences": [], "sizes": []})
        return {"project": "fixture", "rows": rows}

    def inventory(*args):
        found = {"19": ["existing", "v1"]}
        if symbols:
            found["40"] = ["family-40", "v1"]
            found.update({str(symbol.Id.Value): [symbol.UniqueId, "v1"] for symbol in symbols.values()})
        return found

    status = SimpleNamespace(**{value: value for value in ("Uninitialized", "Started", "Committed", "RolledBack")})
    db = SimpleNamespace(Transaction=Transaction, TransactionGroup=Scope, TransactionStatus=status,
                         IFamilyLoadOptions=object, ElementId=int)

    def get_element(key):
        if key == 40 and symbols:
            return family
        return next((symbol for symbol in symbols.values() if symbol.Id.Value == key), None)

    project = SimpleNamespace(GetElement=get_element, Application=SimpleNamespace(DocumentChanged=EventHook()),
                              LoadFamilySymbol=load, Regenerate=lambda: None,
                              Equals=lambda other: other is project)
    monkeypatch.setattr(probe, "read_project", snapshot)
    monkeypatch.setattr(probe, "inventory", inventory)
    group = {"receipt": {"family": "Fixture", "category_id": "-1000", "source_path": "Fixture.rfa",
                         "workspace": "sealed"},
             "rows": [{"line": 2, "type": "Narrow"}, {"line": 3, "type": "Wide"}]}
    from family_library_discovery import manifest_digest

    preview = {"review": {"exports": [group]}}
    args = {"lookup": {}, "preview_sha256": manifest_digest(preview), "baseline_audit_token": "a" * 32}
    baseline = snapshot()
    result = probe.execute_trial(project, db, args, baseline, [("sealed/Fixture.rfa", group)])
    assert result["rollback_verified"] and symbols == {}
    assert attempts == ["Narrow", "Wide"]
    assert project.Application.DocumentChanged == []
    if failure:
        assert result["status"] == "stopped"
    else:
        assert result["status"] == "trial_rolled_back"
        assert result["effects"] == {"added": ["40", "41", "42"], "modified": [], "deleted": []}
        assert result["rows"]["2"]["family_id"] == result["rows"]["3"]["family_id"] == "40"

        import read_batch_family_types as reader
        import verify_batch_family_load as verifier

        monkeypatch.setattr(reader, "inventory", inventory)
        monkeypatch.setattr(reader, "read_project", snapshot)
        monkeypatch.setattr(verifier, "build_load_preview", lambda *args: preview)

        def envelope(run_id, mode, body, decision="no_changes", approval=None):
            changes = {"added_count": 0, "modified_count": 0, "deleted_count": 0, "by_category": {}}
            if mode == "modify":
                added = body["effects"]["added"]
                changes.update(added_count=len(added), added=[{"id": key} for key in added],
                               transactions=[body["transaction"]], by_category={"Family": len(added)})
            return {"run_id": run_id, "mode": mode, "status": "rejected" if decision == "discarded" else "ok",
                    "decision": decision, "approval": approval, "exit_code": 0, "result_truncated": False,
                    "engine": {"requested": "cpython"}, "changes": changes, "blocked": [], "dialogs": [],
                    "failures": [], "warnings": [], "result": body}

        def audit(run_id, transaction=None, ids=None, kept=False):
            request = {"lookup": {}, "audit_inventory": True, "kept": kept}
            if transaction:
                request.update(transaction=transaction, effect_ids=ids)
            return envelope(run_id, "query", reader.project_query(project, db, request))

        before = audit("before")
        before["result"]["audit_token"] = "a" * 32
        trial = envelope("trial", "dry_run", result)
        after = audit("after", result["transaction"], result["effects"]["added"])
        proof = verifier.verify({}, {}, preview, before, trial, after)
        assert proof["status"] == "batch_family_load_rollback_verified"

        stale = dict(proof, baseline_inventory={"element_count": 0, "sha256": "0" * 64})
        with pytest.raises(ValueError, match="project changed"):
            probe.execute_trial(project, db, args, baseline, [("sealed/Fixture.rfa", group)],
                                expected_state=stale)
        assert attempts == ["Narrow", "Wide"]

        def lost_ask():
            raise ValueError("The live ask policy changed.")

        refused = probe.execute_trial(project, db, args, baseline, [("sealed/Fixture.rfa", group)],
                                      handoff=lost_ask, expected_state=proof)
        assert refused["status"] == "stopped" and symbols == {}

        replay = probe.execute_trial(project, db, args, baseline, [("sealed/Fixture.rfa", group)],
                                     handoff=lambda: None, expected_state=proof)
        assert replay["status"] == "awaiting_bridge_decision"
        modify = envelope("modify", "modify", replay, decision="committed", approval="user")
        fresh = audit("fresh", replay["transaction"], replay["effects"]["added"], kept=True)
        checked = verifier.verify_decision({}, {}, preview, before, trial, after, modify, fresh)
        assert checked["status"] == "batch_family_load_keep_verified"
        with pytest.raises(ValueError, match="required status and approval"):
            verifier.verify_decision({}, {}, preview, before, trial, after, dict(modify, approval="auto"), fresh)
        tampered = copy.deepcopy(fresh)
        tampered["result"]["preserved_inventory"]["sha256"] = "0" * 64
        with pytest.raises(ValueError, match="does not prove the recorded decision"):
            verifier.verify_decision({}, {}, preview, before, trial, after, modify, tampered)

        Scope(project, "discard").RollBack()
        modify = envelope("modify", "modify", replay, decision="discarded", approval="user")
        fresh = audit("discard-read", replay["transaction"], replay["effects"]["added"])
        checked = verifier.verify_decision({}, {}, preview, before, trial, after, modify, fresh)
        assert checked["status"] == "batch_family_load_discard_verified"
