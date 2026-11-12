import json
from math import dist
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_room_finish_boundaries.py"


class Seq(list):
    @property
    def Count(self):
        return len(self)


class Point:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z

    def DistanceTo(self, other):
        return dist((self.X, self.Y, self.Z), (other.X, other.Y, other.Z))


class Curve:
    IsBound = True

    def __init__(self, a, b):
        self.ends = Point(*a), Point(*b)

    def GetEndPoint(self, index):
        return self.ends[index]

    def GetType(self):
        return NS(Name="Line")


class CurveLoop(list):
    Append = list.append

    def HasPlane(self):
        return all(curve.ends[0].Z == self[0].ends[0].Z for curve in self)

    def GetPlane(self):
        return NS(Normal=NS(Z=1), Origin=self[0].ends[0])

    def Dispose(self):
        pass


class DotNetList(list):
    Add = list.append


class ListFactory:
    def __getitem__(self, _):
        return DotNetList


class Room:
    Location = object()
    Area = 80
    LevelId = NS(Value=101)
    UniqueId = "synthetic-room"
    DesignOption = None

    def __init__(self, boundary):
        self.boundary = boundary

    def GetBoundarySegments(self, options):
        assert options.SpatialElementBoundaryLocation == "finish"
        return self.boundary

    def get_Parameter(self, name):
        assert name == "room-phase"
        return NS(StorageType="element-id", AsElementId=lambda: 102)


class Level:
    Id = NS(Value=101)
    UniqueId = "synthetic-level"
    ProjectElevation = 25.0


class Phase:
    Id = NS(Value=102)
    UniqueId = "synthetic-phase"


class FloorType:
    UniqueId = "synthetic-floor-type"
    IsFoundationSlab = False


class CeilingType:
    UniqueId = "synthetic-ceiling-type"


def loop(points, closed=True, source=50, linked=-1):
    starts = points if closed else points[:-1]
    ends = points[1:] + points[:1] if closed else points[1:]
    result = Seq()
    for a, b in zip(starts, ends):
        curve = Curve(a, b)
        result.append(NS(GetCurve=lambda c=curve: c, ElementId=NS(Value=source),
                         LinkElementId=NS(Value=linked)))
    return result


def rectangle(x1, y1, x2, y2, z=0):
    return [(x1, y1, z), (x2, y1, z), (x2, y2, z), (x1, y2, z)]


@pytest.fixture
def run(monkeypatch):
    db = NS(
        Architecture=NS(Room=Room), CurveLoop=CurveLoop, ElementId=int,
        SpatialElementBoundaryOptions=lambda: NS(Dispose=lambda: None),
        SpatialElementBoundaryLocation=NS(Finish="finish"),
        BoundaryValidation=NS(IsValidHorizontalBoundary=lambda profiles: bool(profiles)),
        Level=Level, Phase=Phase, FloorType=FloorType, CeilingType=CeilingType,
        BuiltInParameter=NS(ROOM_PHASE_ID="room-phase"), StorageType=NS(ElementId="element-id"),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=ListFactory()))

    def execute(rooms, ids, **extra):
        # The document stand-in has no transaction or write APIs.
        doc = NS(IsFamilyDocument=False, GetElement=lambda item: rooms.get(getattr(item, "Value", item)),
                 PathName="fixture.rvt",
                 ProjectInformation=NS(UniqueId="synthetic-project"))
        result = runpy.run_path(str(SCRIPT), init_globals={"doc": doc, "inputs": dict(extra, room_ids=ids)})
        return json.loads(result["result"]["payload"])

    return execute


def test_preserves_separate_loops_and_linked_boundary_source(run):
    outer = loop(rectangle(0, 0, 8, 8))
    inner = loop(rectangle(2, 2, 3, 3), source=51, linked=71)
    report = run({1: Room(Seq([outer, inner]))}, [1, 1])
    assert report["status"] == "reported"
    assert report["placement_available"] is False
    row, = report["rooms"]
    assert row["status"] == "boundary_verified"
    assert [len(part) for part in row["boundary_loops"]] == [4, 4]
    assert row["boundary_loops"][1][0] == {
        "start_ft": [2, 2, 0], "end_ft": [3, 2, 0], "curve_type": "Line",
        "source_id": 51, "linked_source_id": 71,
    }


def test_excludes_missing_unplaced_open_and_non_coplanar_rooms(run):
    unplaced = Room(None)
    unplaced.Location = None
    open_room = Room(Seq([loop(rectangle(0, 0, 5, 5), closed=False)]))
    different_planes = Room(Seq([loop(rectangle(0, 0, 5, 5)), loop(rectangle(1, 1, 2, 2, z=1))]))
    report = run({2: unplaced, 3: open_room, 4: different_planes}, [1, 2, 3, 4])
    assert report["status"] == "incomplete"
    assert report["rooms"] == [
        {"room_id": 1, "status": "excluded", "reason": "room_not_found"},
        {"room_id": 2, "status": "excluded", "reason": "unplaced_or_unenclosed_room"},
        {"room_id": 3, "status": "excluded", "reason": "open_boundary_loop"},
        {"room_id": 4, "status": "excluded", "reason": "boundary_loops_on_different_planes"},
    ]


def test_rejects_excessive_room_selection_before_query(run):
    with pytest.raises(ValueError, match="1 to 20"):
        run({}, list(range(1, 22)))


def test_horizontal_finish_preview_binds_project_room_level_phase_and_type(run):
    room = Room(Seq([loop(rectangle(0, 0, 8, 8, z=25)), loop(rectangle(2, 2, 3, 3, z=25))]))
    report = run({1: room, 101: Level(), 102: Phase(), 103: FloorType(), 104: CeilingType()}, ["1"],
                 horizontal_finishes={"floor": {"type_id": "103", "offset_ft": -0.125},
                                      "ceiling": {"type_id": "104", "offset_ft": 9.0}})
    assert report["status"] == "reported" and report["placement_available"] is False
    assert report["host_identity"]["project_uid"] == "synthetic-project"
    assert len(report["host_identity"]["path_sha256"]) == 64
    row, = report["rooms"]
    assert row["level"] == {"id": "101", "uid": "synthetic-level", "project_elevation_ft": 25.0}
    assert row["phase"] == {"id": "102", "uid": "synthetic-phase"}
    assert len(row["boundary_loops"]) == 2
    assert row["horizontal_finishes"] == [
        {"kind": "ceiling", "type_id": "104", "type_uid": "synthetic-ceiling-type", "offset_ft": 9.0,
         "project_elevation_ft": 34.0},
        {"kind": "floor", "type_id": "103", "type_uid": "synthetic-floor-type", "offset_ft": -0.125,
         "project_elevation_ft": 24.875},
    ]


def test_horizontal_finish_preview_excludes_room_without_native_scope(run):
    room = Room(Seq([loop(rectangle(0, 0, 8, 8))]))
    room.DesignOption = object()
    report = run({1: room, 103: FloorType()}, ["1"],
                 horizontal_finishes={"floor": {"type_id": "103", "offset_ft": 0}})
    assert report["rooms"] == [{"room_id": 1, "status": "excluded", "reason": "room_design_option_requires_review"}]


@pytest.mark.parametrize("offset", [True, float("inf"), "0"])
def test_horizontal_finish_preview_rejects_invalid_offset(run, offset):
    with pytest.raises(ValueError, match="finite number"):
        run({103: FloorType()}, ["1"], horizontal_finishes={"floor": {"type_id": "103", "offset_ft": offset}})
