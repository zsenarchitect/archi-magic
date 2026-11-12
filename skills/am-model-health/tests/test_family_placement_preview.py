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
