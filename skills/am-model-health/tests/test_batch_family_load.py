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
                         IFamilyLoadOptions=object)
    project = SimpleNamespace(Application=SimpleNamespace(DocumentChanged=EventHook()),
                              LoadFamilySymbol=load, Regenerate=lambda: None,
                              Equals=lambda other: other is project)
    monkeypatch.setattr(probe, "read_project", snapshot)
    monkeypatch.setattr(probe, "inventory", inventory)
    args = {"lookup": {}, "preview_sha256": "fixture"}
    group = {"receipt": {"family": "Fixture", "category_id": "-1000", "source_path": "Fixture.rfa"},
             "rows": [{"line": 2, "type": "Narrow"}, {"line": 3, "type": "Wide"}]}
    result = probe.execute_trial(project, db, args, snapshot(), [("sealed/Fixture.rfa", group)])
    assert result["rollback_verified"] and symbols == {}
    assert attempts == ["Narrow", "Wide"]
    assert project.Application.DocumentChanged == []
    if failure:
        assert result["status"] == "stopped"
    else:
        assert result["status"] == "trial_rolled_back"
        assert result["effects"] == {"added": ["40", "41", "42"], "modified": [], "deleted": []}
        assert result["rows"]["2"]["family_id"] == result["rows"]["3"]["family_id"] == "40"
