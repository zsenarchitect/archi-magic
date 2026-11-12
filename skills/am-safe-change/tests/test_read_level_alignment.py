import json
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from level_alignment_preview import compute


class ElementId:
    def __init__(self, value):
        self.Value = int(value)


class Level(NS):
    pass


class FamilyInstance(NS):
    pass


class LocationPoint(NS):
    pass


class Collector:
    def __init__(self, document):
        self.document = document

    def OfClass(self, element_type):
        return (element for element in self.document.elements.values() if isinstance(element, element_type))


def xyz(z):
    return NS(X=2, Y=3, Z=z)


@pytest.fixture
def model(monkeypatch):
    level_key, offset_key = -1001352, -1001360
    lower = Level(Id=ElementId(1), UniqueId="lower", ProjectElevation=0)
    upper = Level(Id=ElementId(2), UniqueId="upper", ProjectElevation=12)
    doc = NS(
        IsFamilyDocument=False, IsLinked=False, IsReadOnly=False, IsWorkshared=False,
        PathName="synthetic.rvt", ProjectInformation=NS(UniqueId="project"),
        ActiveProjectLocation=NS(UniqueId="location"),
        base=NS(UniqueId="base", Position=xyz(100)), elements={1: lower, 2: upper},
    )
    doc.GetElement = lambda element_id: doc.elements.get(element_id.Value)
    doc.Equals = lambda other: other is doc

    def add(id_value, angle, offset, *, pinned=False):
        parameters = {
            level_key: NS(Id=ElementId(level_key), StorageType="ElementId", HasValue=True,
                          IsReadOnly=False, AsElementId=lambda: lower.Id),
            offset_key: NS(Id=ElementId(offset_key), StorageType="Double", HasValue=True,
                           IsReadOnly=False, AsDouble=lambda: offset,
                           Definition=NS(GetDataType=lambda: NS(Equals=lambda value: value.TypeId == "length"))),
        }
        item = FamilyInstance(
            Id=ElementId(id_value), UniqueId="element-" + str(id_value), Document=doc, ViewSpecific=False,
            Location=LocationPoint(Point=xyz(100 + offset), Rotation=angle), LevelId=lower.Id,
            Host=None, HostFace=None, GroupId=ElementId(-1), Pinned=pinned,
            Symbol=NS(UniqueId="type-a", Family=NS(FamilyPlacementType="OneLevelBased")),
            get_Parameter=parameters.get,
        )
        doc.elements[id_value] = item
        return item

    target = add(101, 0.2, 10)
    reference = add(102, 0.5, -3, pinned=True)
    system = ModuleType("System")
    system.Int64 = int
    api = ModuleType("pyrevit.api")
    api.DB = NS(
        ElementId=ElementId, FamilyInstance=FamilyInstance, Level=Level, LocationPoint=LocationPoint,
        BasePoint=NS(GetProjectBasePoint=lambda document: document.base), FilteredElementCollector=Collector,
        FamilyPlacementType=NS(OneLevelBased="OneLevelBased"),
        BuiltInParameter=NS(FAMILY_LEVEL_PARAM=level_key, INSTANCE_ELEVATION_PARAM=offset_key),
        StorageType=NS(ElementId="ElementId", Double="Double"), SpecTypeId=NS(Length=NS(TypeId="length")),
    )
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setitem(sys.modules, "pyrevit.api", api)

    def query(**request):
        values = runpy.run_path(str(SCRIPTS / "read_level_alignment.py"),
                                init_globals={"doc": doc, "inputs": {"request": request}})
        return values["result"]

    return query, doc, target, reference


def read(query, **request):
    result = query(**request)
    assert result["status"] == "ok", result
    return json.loads(result["snapshot_json"])


def verified_frame(snapshot):
    return {
        "project": snapshot["project"],
        "frame": snapshot["frame"],
        "anchors": [
            {"id": "1", "uid": "lower", "internal_z_ft": 100},
            {"id": "2", "uid": "upper", "internal_z_ft": 112},
        ],
    }


def test_revit_query_drives_rotation_preview_and_preserves_reference(model):
    query, _, target, reference = model
    snapshot = read(query, operation="rotation", target_ids=["101"], reference_id="102")
    preview = compute(snapshot)
    assert preview["after"] == [["101", 2, 3, 110, pytest.approx(0.5), "lower", None]]
    assert snapshot["reference"]["pinned"] is True
    assert preview["ownership_ids"] == ["101"]
    assert target.Location.Rotation == 0.2
    assert reference.Location.Rotation == 0.5


def test_relevel_requires_exact_reviewed_frame_and_pins_native_metadata(model):
    query, document, _, _ = model
    request = {"operation": "nearest_level", "target_ids": ["101"]}
    first = read(query, **request)
    assert first["frame_verified"] is False
    assert compute(first)["blocked"]

    verified = verified_frame(first)
    reviewed = read(query, **request, verified_frame=verified)
    assert reviewed["frame_verified"]
    assert reviewed["targets"][0]["offset_contract"]["offset_parameter_id"] == "-1001360"
    assert compute(reviewed)["after"] == [["101", 2, 3, 110, 0.2, "upper", -2]]

    unmeasured = read(query, **request, verified_frame={"project": first["project"], "frame": first["frame"]})
    assert not unmeasured["frame_verified"]
    document.elements[2].ProjectElevation = 13
    assert not read(query, **request, verified_frame=verified)["frame_verified"]
    document.elements[2].ProjectElevation = 12

    document.base.Position = xyz(101)
    stale = read(query, **request, verified_frame=verified)
    assert not stale["frame_verified"]
    assert compute(stale)["blocked"]


def test_pinned_reference_can_supply_a_comparable_offset(model):
    query, _, _, _ = model
    request = {"operation": "offset", "target_ids": ["101"], "reference_id": "102"}
    first = read(query, **request)
    snapshot = read(query, **request, verified_frame=verified_frame(first))
    assert compute(snapshot)["after"] == [["101", 2, 3, 97, 0.2, "lower", -3]]


def test_unsupported_or_foreign_target_cannot_enter_the_write_scope(model):
    query, document, target, _ = model
    request = {"operation": "rotation", "target_ids": ["101"], "reference_id": "102"}
    target.HostFace = object()
    assert compute(read(query, **request))["blocked"]
    target.HostFace = None
    target.Document = NS()
    assert compute(read(query, **request))["blocked"]
    document.IsLinked = True
    assert query(**request)["status"] == "stopped"
