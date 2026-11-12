from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_library_discovery import manifest_digest
from family_placement_preview import build_preview
from read_family_placement import read_snapshot


def item_id(value):
    return SimpleNamespace(Value=value)


@pytest.fixture
def selection():
    value = {"kind": "family-library-placement-selection", "version": 1, "revit_build": "fixture",
             "family": "Example family", "category": {"id": "-1000"}, "type": "Exact type",
             "source": {"path": "A/Fixture.rfa", "catalog": None}, "placement_type": "OneLevelBased",
             "parameters": [{"name": "Width", "id": "42", "storage": "Double",
                             "data_type": "length", "shared_guid": None}],
             "type_values": {"42": {"state": "value", "value": 2.0}}}
    return dict(value, selection_sha256=manifest_digest(value))


def test_read_reuses_exact_type_and_reports_parameter_change(selection, monkeypatch):
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Int64=int))

    class Level:
        Id = item_id(5)
        UniqueId = "level-uid"
        Name = "Level A"
        ProjectElevation = 20.0

    class Family:
        Id = item_id(18)
        UniqueId = "family-uid"
        FamilyCategory = SimpleNamespace(Id=item_id(-1000))
        FamilyPlacementType = "OneLevelBased"
        Name = "Example family"

        def GetFamilySymbolIds(self):
            return [41, 43]

    class Symbol:
        def __init__(self, identifier, name, family):
            self.Id = item_id(identifier)
            self.UniqueId = "symbol-%s" % identifier
            self.Name = name
            self.Family = family
            self.IsActive = False

        def GetParameters(self, name):
            assert name == "Width"
            return [SimpleNamespace(StorageType="Double", HasValue=True, AsDouble=lambda: 3.0,
                                    Definition=SimpleNamespace(GetDataType=lambda: SimpleNamespace(TypeId="length")))]

    family = Family()
    project = SimpleNamespace(PathName="/fixture/Standalone.rvt", IsFamilyDocument=False, IsReadOnly=False,
                              IsWorkshared=False, IsModelInCloud=False, IsModified=False,
                              ProjectInformation=SimpleNamespace(UniqueId="project-uid"),
                              Application=SimpleNamespace(VersionBuild="fixture"))
    exact = Symbol(41, "Exact type", family)
    elements = {5: Level(), 41: exact, 43: Symbol(43, "exact type", family)}
    project.GetElement = elements.get
    db = SimpleNamespace(Level=Level, Family=Family, FamilySymbol=Symbol, ElementId=int,
                         FilteredElementCollector=lambda _: SimpleNamespace(OfClass=lambda _: [family]))

    def read():
        return {"result": {"snapshot_json": json.dumps(read_snapshot(
            project, db, {"selection": selection, "level_id": "5"}
        ))}}

    preview = build_preview(selection, read(), [1, 2, 23], 450)
    assert preview["kind"] == "change-preview" and preview["blocked"]
    assert preview["after"][0][3:5] == ["reuse", "Required"]
    assert preview["ownership_ids"] == ["41"]
    assert preview["review"]["symbol"]["uid"] == "symbol-41"
    assert preview["review"]["rotation_deg"] == 90
    assert preview["review"]["level_offset_ft"] == 3
    assert preview["parameter_differences"][0]["project"] == {"state": "value", "value": 3.0}
    assert manifest_digest(preview["review"]) == preview["review_sha256"]

    exact.IsActive = True
    assert build_preview(selection, read(), [1, 2, 23], 0)["ownership_ids"] == []

    exact.Name = "Different type"
    absent = build_preview(selection, read(), [1, 2, 23], 0)
    assert absent["review"]["action"] == "load"
    assert any("already exists" in warning["text"] for warning in absent["warnings"])


def test_rejects_snapshot_for_different_selection(selection):
    read = {"kind": "family-placement-project-snapshot", "version": 1,
            "selection_sha256": "different", "revit_build": selection["revit_build"]}
    with pytest.raises(ValueError, match="does not match"):
        build_preview(selection, read, [1, 2, 23], 0)


@pytest.mark.parametrize("extra_effect", [False, True])
def test_reuse_trial_rolls_back_after_provisional_effect_check(selection, monkeypatch, extra_effect):
    import math
    import probe_family_placement as probe

    selection.update(generation="test-generation")
    selection.pop("selection_sha256")
    selection["selection_sha256"] = manifest_digest(selection)
    symbol_row = {"id": "41", "uid": "symbol-41", "active": False, "placement_type": "OneLevelBased",
                  "parameters": {"42": {"state": "value", "value": 2.0}}}
    baseline = {
        "kind": "family-placement-project-snapshot", "version": 1, "revit_build": "fixture",
        "selection_sha256": selection["selection_sha256"], "project": {"project_uid": "fixture"},
        "level": {"id": "5", "uid": "level-5", "name": "Level A", "project_elevation": 20},
        "same_name_families": [], "exact_symbols": [symbol_row],
    }
    preview = build_preview(selection, baseline, [1, 2, 23], 90)
    args = {"index": "fixture-index", "disposable_fixture": True, "preview": preview,
            "preview_sha256": manifest_digest(preview)}

    class XYZ:
        def __init__(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z

        def __add__(self, other):
            return XYZ(self.X + other.X, self.Y + other.Y, self.Z + other.Z)

    XYZ.BasisZ = XYZ(0, 0, 1)

    class EventHook(list):
        def __iadd__(self, handler):
            self.append(handler)
            return self

        def __isub__(self, handler):
            self.remove(handler)
            return self

    class Scope:
        def __init__(self, document, name):
            self.state = "Uninitialized"

        def Start(self):
            self.state = "Started"
            return self.state

        def GetStatus(self):
            return self.state

        def Dispose(self):
            pass

    class Group(Scope):
        def RollBack(self):
            symbol.IsActive = False
            elements.pop(99, None)
            self.state = "RolledBack"
            return self.state

    class Transaction(Scope):
        def Commit(self):
            self.state = "Committed"
            event = SimpleNamespace(
                Operation="TransactionCommitted", GetDocument=lambda: project,
                GetTransactionNames=lambda: [probe.TRANSACTION_NAME],
                GetAddedElementIds=lambda: [item_id(99)], GetDeletedElementIds=lambda: [],
                GetModifiedElementIds=lambda: [item_id(41), item_id(5)] if extra_effect else [item_id(41)],
            )
            for handler in project.Application.DocumentChanged:
                handler(None, event)
            return self.state

    level = SimpleNamespace(Id=item_id(5), UniqueId="level-5")
    symbol = SimpleNamespace(Id=item_id(41), UniqueId="symbol-41", IsActive=False)
    symbol.Activate = lambda: setattr(symbol, "IsActive", True)
    elements = {5: level, 41: symbol}

    def place(insertion, chosen, base, structural):
        assert (insertion.X, insertion.Y, insertion.Z) == (1, 2, 3)
        instance = SimpleNamespace(Id=item_id(99), UniqueId="instance-99", Host=None,
                                   Symbol=chosen, LevelId=base.Id,
                                   Location=SimpleNamespace(Point=XYZ(1, 2, 23), Rotation=0))
        elements[99] = instance
        return instance

    def rotate(document, identifier, axis, angle):
        assert math.isclose(angle, math.pi / 2)
        elements[identifier.Value].Location.Rotation = angle

    project = SimpleNamespace(IsModifiable=False, Application=SimpleNamespace(DocumentChanged=EventHook()),
                              GetElement=elements.get, Regenerate=lambda: None,
                              Equals=lambda other: other is project,
                              Create=SimpleNamespace(NewFamilyInstance=place))
    statuses = SimpleNamespace(**{key: key for key in ("Uninitialized", "Started", "Committed", "RolledBack")})
    db = SimpleNamespace(TransactionStatus=statuses, Transaction=Transaction, TransactionGroup=Group,
                         ElementId=int, XYZ=XYZ, LocationPoint=SimpleNamespace,
                         Structure=SimpleNamespace(StructuralType=SimpleNamespace(NonStructural=0)),
                         Line=SimpleNamespace(CreateBound=lambda start, end: (start, end)),
                         ElementTransformUtils=SimpleNamespace(RotateElement=rotate))
    monkeypatch.setattr(probe, "select_for_placement", lambda *args: selection)
    monkeypatch.setattr(probe, "read_snapshot", lambda *args, **kwargs: dict(
        baseline, exact_symbols=[dict(symbol_row, active=symbol.IsActive)]
    ))
    monkeypatch.setattr(probe, "inventory", lambda *args: {
        str(key): [value.UniqueId, "fingerprint"] for key, value in elements.items()
    })
    result = probe.trial(project, db, args)
    assert result["rollback_verified"] is True
    assert result["status"] == ("stopped" if extra_effect else "trial_rolled_back")
    assert set(elements) == {5, 41} and symbol.IsActive is False
    assert not project.Application.DocumentChanged
    if not extra_effect:
        assert result["placement"]["xyz_ft"] == [1, 2, 23]
