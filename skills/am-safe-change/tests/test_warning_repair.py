import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import warning_repair_preview as preview

DUPLICATE = "11111111-0000-4000-8000-000000000001"
PERFORMANCE = "11111111-0000-4000-8000-000000000002"
SEPARATOR = "11111111-0000-4000-8000-000000000003"


class Id:
    def __init__(self, value):
        self.Value = value


class Level:
    Id = Id(30)
    ProjectElevation = 0.0


def xyz(x, y=0, z=0):
    return NS(X=x, Y=y, Z=z)


class LocationPoint:
    Point = xyz(0)
    Rotation = 0.0


class Instance:
    Category = NS(Id=Id(-3))
    Location = LocationPoint()
    Symbol = NS(Id=Id(40), Family=NS(IsInPlace=False, FamilyPlacementType="level"))
    Host = HostFace = SuperComponent = MEPModel = DesignOption = None
    FacingFlipped = HandFlipped = Mirrored = False
    Pinned = ViewSpecific = False
    LevelId = Id(30)
    GroupId = AssemblyInstanceId = DemolishedPhaseId = Id(-1)
    CreatedPhaseId = Id(31)
    WorksetId = NS(IntegerValue=3)
    Parameters = [NS(Id=Id(50), StorageType="String", IsShared=False, HasValue=True,
                     AsString=lambda: "Fixture")]

    def __init__(self, identifier):
        self.Id = Id(identifier)
        self.UniqueId = "instance-%d" % identifier

    def GetSubComponentIds(self):
        return []

    def GetDependentElements(self, _):
        return []

    def GetEntitySchemaGuids(self):
        return []

    def GetTransform(self):
        return NS(Origin=xyz(0), BasisX=xyz(1), BasisY=xyz(0, 1), BasisZ=xyz(0, 0, 1))


class Line:
    IsBound = True

    def __init__(self, start, end):
        self.points = [start, end]

    def GetEndPoint(self, end):
        return self.points[end]


class Arc(Line):
    def __init__(self, start, middle, end):
        super().__init__(start, end)
        self.middle = middle

    def Evaluate(self, parameter, normalized):
        assert parameter == 0.5 and normalized
        return self.middle


class ModelLine(Instance):
    Category = NS(Id=Id(-4))
    SketchPlane = NS(Id=Id(62), GetPlane=lambda: NS(Origin=xyz(0), Normal=xyz(0, 0, 1)))

    def __init__(self, identifier, start, end):
        super().__init__(identifier)
        self.GeometryCurve = Line(start, end)

    def GetAdjoinedCurveElements(self, _):
        return []


class Room:
    LevelId = Id(30)
    CreatedPhaseId = Id(31)
    Location = LocationPoint()
    DesignOption = None

    def __init__(self, identifier, area, loops):
        self.Id = Id(identifier)
        self.UniqueId = "room-%d" % identifier
        self.Area = area
        self.loops = loops

    def GetBoundarySegments(self, _):
        return self.loops


def segment(curve, owner, link=-1):
    return NS(GetCurve=lambda: curve, ElementId=Id(owner), LinkElementId=Id(link))


def warning(kind, failing, additional=()):
    return NS(GetFailureDefinitionId=lambda: NS(Guid=kind),
              GetFailingElements=lambda: [Id(item) for item in failing],
              GetAdditionalElements=lambda: [Id(item) for item in additional])


class Collector:
    def __init__(self, document):
        self.document = document

    def OfCategory(self, category):
        assert category == -5
        return self

    def WhereElementIsNotElementType(self):
        return iter(self.document.rooms)


@pytest.fixture
def runtime(monkeypatch):
    db = NS(ElementId=Id, Level=Level, LocationPoint=LocationPoint, FamilyInstance=Instance, ModelLine=ModelLine,
            Line=Line, Arc=Arc, Architecture=NS(Room=Room), FilteredElementCollector=Collector,
            SpatialElementBoundaryOptions=lambda: NS(Dispose=lambda: None),
            SpatialElementBoundaryLocation=NS(Finish="finish"),
            FamilyPlacementType=NS(OneLevelBased="level"),
            BuiltInCategory=NS(OST_Furniture=-3, OST_RoomSeparationLines=-4, OST_Rooms=-5),
            BuiltInFailures=NS(OverlapFailures=NS(DuplicateInstances=NS(Guid=DUPLICATE),
                                                 RoomSeparationLinesOverlap=NS(Guid=SEPARATOR)),
                               PerformanceFailures=NS(DuplicateInstances=NS(Guid=PERFORMANCE))))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    script = runpy.run_path(str(SCRIPTS / "warning_repair.py"))
    elements = {1: Instance(1), 2: Instance(2), 30: Level(),
                3: ModelLine(3, xyz(0), xyz(1)), 4: ModelLine(4, xyz(1), xyz(0))}
    document = NS(PathName="/fixture.rvt", IsFamilyDocument=False, IsReadOnly=False, IsWorkshared=False,
                  ProjectInformation=NS(UniqueId="project"), elements=elements, rooms=[])
    document.GetElement = lambda native: elements.get(native.Value)
    document.warnings = [warning(DUPLICATE, [2, 1]), warning(SEPARATOR, [3, 4])]
    document.GetWarnings = lambda: document.warnings
    return script, document


def read(runtime, kind, keep, remove):
    script, document = runtime
    request = {"pairs": [{"failure_id": kind, "keep_id": str(keep), "remove_id": str(remove)}]}
    result = script["main"](document, {"request": request})
    return preview.read_snapshot({"result": result})


def test_native_reader_feeds_planner_and_preserves_additional_refs(runtime):
    snapshot = read(runtime, DUPLICATE, 1, 2)
    plan, _ = preview.compute(snapshot)
    assert plan["steps"][0]["remove_uid"] == "instance-2"
    assert snapshot["elements"]["2"]["equivalence"]["context"]["workset"] == "3"
    _, document = runtime
    document.warnings[0] = warning(DUPLICATE, [1, 2], [2])
    snapshot = read(runtime, DUPLICATE, 1, 2)
    assert snapshot["warnings"][0][-1] == ["2"]
    assert preview.compute(snapshot)[0]["blocked"]


def test_rooms_keep_enclosure_and_linked_source_in_snapshot(runtime):
    script, document = runtime
    a, b, c = xyz(0), xyz(1), xyz(0, 1)
    first = segment(Arc(a, xyz(0.5, -0.2), b), 4)
    linked = segment(Line(b, c), 4, link=40)
    last = segment(Line(c, a), 62)
    room = Room(11, 3.0, [[first, linked, last]])
    document.rooms = [Room(10, 0.0, None), room]
    snapshot = read(runtime, SEPARATOR, 3, 4)
    assert preview.compute(snapshot)[0]["steps"][0]["recipe"] == "coincident-room-separators"
    assert snapshot["rooms"]["10"]["enclosed"] is False
    clock = NS(ElapsedMilliseconds=0)
    aliased = script["room_inventory"](document, clock, {"4": "3"})
    sources = {(owner, link) for owner, link, _ in aliased["11"]["loops"][0]}
    assert sources == {("3", "-1"), ("4", "40"), ("62", "-1")}
    room.loops[0][0] = segment(Arc(a, xyz(0.5, -0.4), b), 4)
    changed = read(runtime, SEPARATOR, 3, 4)
    assert changed["rooms"]["11"]["area"] == snapshot["rooms"]["11"]["area"]
    assert preview.snapshot_digest(changed) != preview.snapshot_digest(snapshot)


def test_line_with_different_extent_or_unknown_room_curve_cannot_clear(runtime):
    _, document = runtime
    document.elements[4].GeometryCurve = Line(xyz(0), xyz(2))
    assert preview.compute(read(runtime, SEPARATOR, 3, 4))[0]["blocked"]
    document.rooms = [Room(11, 3.0, [[segment(object(), 3)]])]
    with pytest.raises(ValueError, match="geometry that cannot be verified"):
        read(runtime, SEPARATOR, 3, 4)


class Event:
    def __init__(self):
        self.listeners = []

    def __iadd__(self, callback):
        self.listeners.append(callback)
        return self

    def __isub__(self, callback):
        self.listeners.remove(callback)
        return self

    def fire(self, args):
        for callback in self.listeners:
            callback(None, args)


class FailureOptions:
    guard = None
    clear = False

    def SetFailuresPreprocessor(self, value):
        self.guard = value

    def GetFailuresPreprocessor(self):
        return self.guard

    def SetClearAfterRollback(self, value):
        self.clear = value

    def GetClearAfterRollback(self):
        return self.clear

    def Dispose(self):
        pass


class Transaction:
    status = "uninitialized"

    def __init__(self, document, name):
        self.document, self.name = document, name
        self.options = FailureOptions()

    def Start(self):
        self.status = "started"
        return self.status

    def GetStatus(self):
        return self.status

    def RollBack(self):
        self.status = "rolled back"
        return self.status

    def GetFailureHandlingOptions(self):
        return self.options

    def SetFailureHandlingOptions(self, options):
        self.options = options

    def Commit(self):
        doc = self.document
        if doc.pending:
            self.status = "pending"
            return self.status
        decision = self.options.guard.PreprocessFailures(NS(GetFailureMessages=lambda: doc.posted))
        if decision == "rollback":
            return self.RollBack()
        self.status = "committed"
        if doc.commit_hook:
            doc.commit_hook()
        if doc.emit_event:
            doc.Application.DocumentChanged.fire(
                NS(GetDocument=lambda: doc, GetTransactionNames=lambda: [self.name],
                   GetAddedElementIds=lambda: [], GetModifiedElementIds=lambda: doc.modified,
                   GetDeletedElementIds=lambda: doc.deleted)
            )
        return self.status


class TransactionGroup(Transaction):
    def Start(self):
        doc = self.document
        self.saved_elements = dict(doc.elements)
        self.saved_warnings = list(doc.warnings)
        self.saved_rooms = [(room, room.Area, room.loops) for room in doc.rooms]
        return super().Start()

    def RollBack(self):
        doc = self.document
        doc.elements.clear()
        doc.elements.update(self.saved_elements)
        doc.warnings[:] = self.saved_warnings
        for room, area, loops in self.saved_rooms:
            room.Area, room.loops = area, loops
        return super().RollBack()

    def Assimilate(self):
        self.status = "committed"
        return self.status


@pytest.fixture
def writer(runtime):
    script, doc = runtime
    db = script["DB"]
    db.Transaction, db.TransactionGroup = Transaction, TransactionGroup
    db.IFailuresPreprocessor = object
    db.FailureProcessingResult = NS(Continue="continue", ProceedWithRollBack="rollback")
    db.TransactionStatus = NS(Started="started", Pending="pending", Committed="committed", RolledBack="rolled back")
    doc.Application = NS(DocumentChanged=Event())
    doc.Equals = lambda value: doc is value
    doc.posted, doc.deleted, doc.modified = [], [], []
    doc.pending, doc.emit_event, doc.commit_hook = False, True, None

    def delete(native_id):
        doc.elements.pop(native_id.Value)
        doc.deleted.append(native_id)
        doc.warnings[:] = [row for row in doc.warnings
                           if native_id.Value not in {item.Value for item in row.GetFailingElements()}]
        return [native_id]

    doc.Delete = delete
    return script, doc


def apply_plan(runtime, kind=DUPLICATE, keep=1, remove=2):
    script, doc = runtime
    plan, display = preview.compute(read(runtime, kind, keep, remove))
    return script["main"](doc, {"mode": "apply", "plan": plan, "preview": display})


def test_write_preserves_exact_remaining_warnings(writer):
    result = apply_plan(writer)
    assert result["status"] == "ok"
    receipt = json.loads(result["receipt_json"])
    assert receipt["deleted"] == ["2"]
    assert receipt["warnings"] == [[SEPARATOR, ["3", "4"], []]]
    assert 1 in writer[1].elements and 2 not in writer[1].elements


@pytest.mark.parametrize("effect", ["cascade", "posted_warning", "modified", "missing_event", "changed_warnings"])
def test_write_rolls_back_unapproved_effects(writer, effect):
    _, doc = writer
    original = doc.Delete
    if effect == "cascade":
        doc.Delete = lambda item: original(item) + [Id(99)]
    elif effect == "posted_warning":
        posted = warning(PERFORMANCE, [3, 4])
        posted.GetSeverity = lambda: "Warning"
        doc.posted = [posted]
    elif effect == "modified":
        doc.modified = [Id(1)]
    elif effect == "missing_event":
        doc.emit_event = False
    else:
        doc.commit_hook = lambda: doc.warnings.pop()
    result = apply_plan(writer)
    assert result["status"] == "stopped" and result["target_rollback_verified"]
    if effect == "posted_warning":
        assert result["posted_failures"] == [[PERFORMANCE, "Warning"]]
    assert set(doc.elements) == {1, 2, 3, 4, 30}
    assert len(doc.warnings) == 2
    assert doc.Application.DocumentChanged.listeners == []


def test_repair_treats_pending_as_unresolved(writer):
    writer[1].pending = True
    result = apply_plan(writer)
    assert result["status"] == "transaction_unresolved"
    assert "target_rollback_verified" not in result


@pytest.mark.parametrize("change", ["approved_source_swap", "same_area_changed_arc", "stale_deleted_source"])
def test_separator_repair_verifies_boundary_topology_before_finishing(writer, change):
    _, doc = writer
    a, b, c = xyz(0), xyz(1), xyz(0, 1)
    room = Room(11, 3.0, [[segment(Arc(a, xyz(0.5, -0.2), b), 4),
                           segment(Line(b, c), 61), segment(Line(c, a), 62)]])
    doc.rooms = [room]

    def on_commit():
        source = 4 if change == "stale_deleted_source" else 3
        arc_offset = -0.4 if change == "same_area_changed_arc" else -0.2
        room.loops = [[segment(Arc(a, xyz(0.5, arc_offset), b), source),
                       segment(Line(b, c), 61), segment(Line(c, a), 62)]]

    doc.commit_hook = on_commit
    result = apply_plan(writer, SEPARATOR, 3, 4)
    if change == "approved_source_swap":
        assert result["status"] == "ok" and 4 not in doc.elements
    else:
        assert result["status"] == "stopped" and result["target_rollback_verified"]
        assert "room boundary differs" in result["message"] and 4 in doc.elements
        assert room.loops[0][0].ElementId.Value == 4


def test_only_reviewed_worksharing_borrowing_can_survive_recheck(writer):
    script, doc = writer
    doc.IsWorkshared = True
    owners = {1: "NotOwned", 2: "NotOwned"}
    updates = {1: "CurrentWithCentral", 2: "CurrentWithCentral"}
    script["DB"].WorksharingUtils = NS(GetCheckoutStatus=lambda _doc, item: owners[item.Value],
                                      GetModelUpdatesStatus=lambda _doc, item: updates[item.Value])
    plan, display = preview.compute(read(writer, DUPLICATE, 1, 2))
    owners[2] = "OwnedByCurrentUser"
    args = {"mode": "apply", "plan": plan, "preview": display}
    with pytest.raises(ValueError, match="no longer matches"):
        script["main"](doc, args)
    updates[2] = "UpdatedInCentral"
    args["approved_borrowing"] = ["2"]
    with pytest.raises(ValueError, match="borrowing does not match"):
        script["main"](doc, args)
    updates[2] = "CurrentWithCentral"
    result = script["main"](doc, args)
    assert result["status"] == "ok"
    assert json.loads(result["receipt_json"])["borrowed_after_dry_run"] == ["2"]
