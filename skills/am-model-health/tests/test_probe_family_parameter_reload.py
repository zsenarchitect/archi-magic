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
