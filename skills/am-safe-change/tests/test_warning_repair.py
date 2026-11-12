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
