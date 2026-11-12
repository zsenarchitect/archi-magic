import json
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from level_alignment_preview import compute, probe_plan


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
        IsFamilyDocument=False, IsLinked=False, IsReadOnly=False, IsWorkshared=False, IsModifiable=False,
        PathName="synthetic.rvt", ProjectInformation=NS(UniqueId="project"),
        ActiveProjectLocation=NS(UniqueId="location"),
        base=NS(UniqueId="base", Position=xyz(100)), elements={1: lower, 2: upper},
    )
    doc.GetElement = lambda element_id: doc.elements.get(element_id.Value)
    doc.Equals = lambda other: other is doc

    def add(id_value, angle, offset, *, pinned=False):
        item = FamilyInstance(
            Id=ElementId(id_value), UniqueId="element-" + str(id_value), Document=doc, ViewSpecific=False,
            Location=LocationPoint(Point=xyz(100 + offset), Rotation=angle), LevelId=lower.Id, Offset=offset,
            Host=None, HostFace=None, GroupId=ElementId(-1), Pinned=pinned,
            Symbol=NS(UniqueId="type-a", Family=NS(FamilyPlacementType="OneLevelBased")),
        )

        def set_level(value):
            item.LevelId = value
            item.Location.Point.Z = doc.base.Position.Z + doc.GetElement(value).ProjectElevation + item.Offset
            return True

        def set_offset(value):
            item.Offset = value
            item.Location.Point.Z = doc.base.Position.Z + doc.GetElement(item.LevelId).ProjectElevation + value
            return True

        parameters = {
            level_key: NS(Id=ElementId(level_key), StorageType="ElementId", HasValue=True, Set=set_level,
                          IsReadOnly=False, AsElementId=lambda: item.LevelId),
            offset_key: NS(Id=ElementId(offset_key), StorageType="Double", HasValue=True, Set=set_offset,
                           IsReadOnly=False, AsDouble=lambda: item.Offset,
                           Definition=NS(GetDataType=lambda: NS(Equals=lambda value: value.TypeId == "length"))),
        }
        item.get_Parameter = parameters.get
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


class Changes:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def send(self, doc, name, operation, ids):
        class Ids(list):
            Count = property(len)

        event = NS(
            Operation=operation, GetDocument=lambda: doc, GetTransactionNames=lambda: [name],
            GetAddedElementIds=Ids, GetDeletedElementIds=Ids,
            GetModifiedElementIds=lambda: Ids(ElementId(value) for value in ids),
        )
        for handler in self.handlers:
            handler(None, event)


class Vector(NS):
    def __init__(self, x, y, z):
        super().__init__(X=x, Y=y, Z=z)

    def __add__(self, other):
        return Vector(self.X + other.X, self.Y + other.Y, self.Z + other.Z)


Vector.BasisZ = Vector(0, 0, 1)


@pytest.fixture
def probe(model, monkeypatch):
    query, doc, _, _ = model
    event = Changes()
    doc.Application = NS(DocumentChanged=event)
    doc.Regenerate = lambda: None
    log = []
    mode = NS(extra=False, rollback_failure=False, fail_target=None)
    status = NS(Uninitialized="Uninitialized", Started="Started", Committed="Committed", RolledBack="RolledBack")

    class Group:
        def __init__(self, document, name):
            self.document, self.name = document, name
            self.state = status.Uninitialized

        def GetStatus(self):
            return self.state

        def state_of_model(self):
            return {
                id_value: (item.LevelId.Value, item.Offset, item.Location.Point.Z, item.Location.Rotation)
                for id_value, item in self.document.elements.items() if isinstance(item, FamilyInstance)
            }

        def Start(self):
            self.baseline = self.state_of_model()
            self.state = status.Started
            return self.state

        def RollBack(self):
            if mode.rollback_failure:
                raise RuntimeError("fixture rollback failure")
            for id_value, (level, offset, z, rotation) in self.baseline.items():
                item = self.document.elements[id_value]
                item.LevelId, item.Offset = ElementId(level), offset
                item.Location.Point.Z, item.Location.Rotation = z, rotation
            self.state = status.RolledBack
            return self.state

    class Transaction(Group):
        def Commit(self):
            now = self.state_of_model()
            modified = [id_value for id_value, value in now.items() if self.baseline[id_value] != value]
            if mode.extra:
                modified.append(102)
            self.state = status.Committed
            log.append(("commit", modified))
            event.send(doc, self.name, "TransactionCommitted", modified)
            return self.state

    def rotate(document, element_id, axis, angle):
        if element_id.Value == mode.fail_target:
            raise ValueError("fixture rotation failure")
        document.GetElement(element_id).Location.Rotation += angle

    api = sys.modules["pyrevit.api"].DB
    api.TransactionGroup, api.Transaction, api.TransactionStatus = Group, Transaction, status
    api.XYZ = Vector
    api.Line = NS(CreateBound=lambda start, end: (start, end))
    api.ElementTransformUtils = NS(RotateElement=rotate)
    exceptions = ModuleType("Autodesk.Revit.Exceptions")
    exceptions.RegenerationFailedException = type("RegenerationFailedException", (Exception,), {})
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.Exceptions", exceptions)

    def run(plan):
        values = runpy.run_path(str(SCRIPTS / "probe_level_alignment.py"),
                                init_globals={"doc": doc, "inputs": {"plan": plan, "disposable_fixture": True}})
        assert not event.handlers
        return values["result"]

    return query, doc, run, mode, log


@pytest.mark.parametrize("operation", ["rotation", "offset", "nearest_level"])
def test_native_probe_checks_all_three_operations_then_restores_the_baseline(probe, operation):
    query, _, run, _, log = probe
    request = {"operation": operation, "target_ids": ["101"]}
    if operation != "nearest_level":
        request["reference_id"] = "102"
    first = read(query, **request)
    if operation != "rotation":
        request["verified_frame"] = verified_frame(first)
    baseline = read(query, **request)
    plan = probe_plan(baseline)
    result = run(plan)
    assert result["status"] == "trial_rolled_back", result
    assert result["modified_ids"] == ["101"]
    measurement = result["measurements"]["101"]
    expected = plan["changes"]["101"]
    assert measurement["point"] == expected["point"]
    assert measurement["rotation_rad"] == pytest.approx(expected["rotation_rad"])
    assert measurement["level_uid"] == expected["level_uid"]
    assert measurement["offset_ft"] == expected["offset_ft"]
    assert read(query, **request) == baseline
    assert log == [("commit", [101])]


def test_stale_reference_and_tampered_plan_stop_before_transaction(probe):
    query, document, run, _, log = probe
    request = {"operation": "rotation", "target_ids": ["101"], "reference_id": "102"}
    plan = probe_plan(read(query, **request))
    assert run(dict(plan, basis_sha256="0" * 64))["status"] == "stopped"
    document.elements[102].Location.Rotation += 0.2
    assert run(plan)["status"] == "stopped"
    assert log == []


def test_unapproved_effect_rolls_back_even_after_provisional_commit(probe):
    query, _, run, mode, log = probe
    request = {"operation": "rotation", "target_ids": ["101"], "reference_id": "102"}
    baseline = read(query, **request)
    mode.extra = True
    result = run(probe_plan(baseline))
    assert result["status"] == "stopped"
    assert result["rollback_verified"] is True
    assert log == [("commit", [101, 102])]
    assert read(query, **request) == baseline


def test_late_target_failure_restores_earlier_target_before_commit(probe):
    query, document, run, mode, log = probe
    source = document.elements[101]
    document.elements[103] = FamilyInstance(**dict(vars(source), Id=ElementId(103), UniqueId="element-103",
                                                   Location=LocationPoint(Point=xyz(111), Rotation=0.3)))
    request = {"operation": "rotation", "target_ids": ["101", "103"], "reference_id": "102"}
    baseline = read(query, **request)
    mode.fail_target = 103
    result = run(probe_plan(baseline))
    assert result["status"] == "stopped"
    assert result["rollback_verified"] is True
    assert read(query, **request) == baseline
    assert log == []


def test_failed_rollback_reports_an_unresolved_transaction(probe):
    query, _, run, mode, _ = probe
    request = {"operation": "rotation", "target_ids": ["101"], "reference_id": "102"}
    mode.rollback_failure = True
    result = run(probe_plan(read(query, **request)))
    assert result["status"] == "transaction_unresolved"
    assert "rollback_verified" not in result


def test_wrong_elevation_with_correct_level_and_offset_cannot_commit(probe):
    query, document, run, _, log = probe
    request = {"operation": "nearest_level", "target_ids": ["101"]}
    first = read(query, **request)
    request["verified_frame"] = verified_frame(first)
    baseline = read(query, **request)
    target = document.elements[101]

    def regenerate():
        if target.LevelId.Value == 2 and target.Offset == -2:
            target.Location.Point.Z += 0.1

    document.Regenerate = regenerate
    result = run(probe_plan(baseline))
    assert result["status"] == "stopped"
    assert "reviewed position" in result["message"]
    assert result["rollback_verified"] is True
    assert log == []
    assert read(query, **request) == baseline
