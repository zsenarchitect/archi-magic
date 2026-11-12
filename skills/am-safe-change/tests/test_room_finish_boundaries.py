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


class WallType:
    UniqueId = "synthetic-wall-type"
    Kind = "basic"
    Width = 0.125


class Wall:
    WallType = WallType()
    CrossSection = "vertical"
    IsStackedWallMember = False
    DesignOption = None

    def __init__(self, value, inserts=()):
        self.Id = NS(Value=value)
        self.UniqueId = "fixture-wall-" + str(value)
        self.inserts = [NS(Value=i) for i in inserts]

    def GetPhaseStatus(self, phase_id):
        assert phase_id.Value == 102
        return "existing"

    def FindInserts(self, *flags):
        assert flags == (True, True, True, True)
        return self.inserts


class Door:
    DesignOption = None
    Category = NS(Id=NS(Value=-42))

    def __init__(self, value, wall, low, high):
        self.UniqueId = "fixture-door-" + str(value)
        self.Host = wall
        self.bounds = NS(Enabled=True, Transform=NS(IsIdentity=True),
                         Min=Point(*low), Max=Point(*high))

    def GetPhaseStatus(self, phase_id):
        assert phase_id.Value == 102
        return "new"

    def get_BoundingBox(self, view):
        assert view is None
        return self.bounds


def loop(points, closed=True, source=50, linked=-1):
    starts = points if closed else points[:-1]
    ends = points[1:] + points[:1] if closed else points[1:]
    result = Seq()
    for i, (a, b) in enumerate(zip(starts, ends)):
        curve = Curve(a, b)
        wall = source[i] if isinstance(source, list) else source
        result.append(NS(GetCurve=lambda c=curve: c, ElementId=NS(Value=wall),
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
        Wall=Wall, WallType=WallType, FamilyInstance=Door, WallKind=NS(Basic="basic"),
        WallCrossSection=NS(Vertical="vertical"), BuiltInCategory=NS(OST_Doors=-42),
        ElementOnPhaseStatus=NS(New="new", Existing="existing"),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=ListFactory()))

    def execute(rooms, ids, **extra):
        # The document stand-in has no transaction or write APIs.
        doc = NS(IsFamilyDocument=False, GetElement=lambda item: rooms.get(getattr(item, "Value", item)),
                 PathName="fixture.rvt", Application=NS(ShortCurveTolerance=0.01),
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


def test_wall_finish_spans_skip_overlapping_door_envelopes(run):
    wall = Wall(50, [151, 152])
    room = Room(Seq([loop(rectangle(0, 0, 10, 8), source=[50, 51, 52, 53])]))
    report = run({1: room, 50: wall, 51: Wall(51), 52: Wall(52), 53: Wall(53), 101: Level(),
                  102: Phase(), 103: WallType(), 151: Door(151, wall, (2, -0.2, 0), (5, 0.2, 7)),
                  152: Door(152, wall, (4, -0.2, 0), (6, 0.2, 7))}, ["1"], wall_type_id="103")
    assert report["status"] == "reported" and report["placement_available"] is False
    row, = report["rooms"]
    assert row["wall_finish"] == {"type_id": "103", "type_uid": "synthetic-wall-type",
                                  "width_ft": 0.125, "status": "reported"}
    first, second, _, _ = row["boundary_loops"][0]
    assert first["wall_finish"]["boundary_spans_ft"] == [[0.0, 2.0], [6.0, 10.0]]
    assert first["wall_finish"]["door_envelopes"] == [
        {"id": "151", "uid": "fixture-door-151", "start_ft": 2.0, "end_ft": 5.0},
        {"id": "152", "uid": "fixture-door-152", "start_ft": 4.0, "end_ft": 6.0},
    ]
    assert second["wall_finish"]["boundary_spans_ft"] == [[0.0, 8.0]]
    assert first["wall_finish"]["finish_footprints"] == [
        {"status": "footprint_previewed", "span_ft": [0.125, 2.0], "original_span_ft": [0.0, 2.0],
         "centerline_ft": [[0.125, 0.0625, 0], [2.0, 0.0625, 0]]},
        {"status": "footprint_previewed", "span_ft": [6.0, 9.875], "original_span_ft": [6.0, 10.0],
         "centerline_ft": [[6.0, 0.0625, 0], [9.875, 0.0625, 0]]},
    ]


def test_wall_finish_chooses_room_side_independent_of_segment_direction(run):
    outer = list(reversed(rectangle(0, 0, 10, 8)))
    room = Room(Seq([loop(outer, source=[50, 51, 52, 53])]))
    report = run({1: room, 50: Wall(50), 51: Wall(51), 52: Wall(52), 53: Wall(53),
                  101: Level(), 102: Phase(), 103: WallType()}, ["1"], wall_type_id="103")
    first = report["rooms"][0]["boundary_loops"][0][0]["wall_finish"]
    assert first["finish_footprints"][0]["centerline_ft"] == [[0.0, 7.9375, 0], [10.0, 7.9375, 0]]


@pytest.mark.parametrize("reverse", [False, True])
def test_wall_finish_trims_overlaps_at_each_rectangular_corner(run, reverse):
    outline = rectangle(0, 0, 10, 8)
    room = Room(Seq([loop(list(reversed(outline)) if reverse else outline, source=[50, 51, 52, 53])]))
    report = run({1: room, 50: Wall(50), 51: Wall(51), 52: Wall(52), 53: Wall(53),
                  101: Level(), 102: Phase(), 103: WallType()}, ["1"], wall_type_id="103")
    assert report["status"] == "reported"
    pieces = [piece for segment in report["rooms"][0]["boundary_loops"][0]
              for piece in segment["wall_finish"]["finish_footprints"]]
    assert sorted(sorted(piece["centerline_ft"]) for piece in pieces) == [
        [[0.0, 0.0625, 0], [10.0, 0.0625, 0]],
        [[0.0, 7.9375, 0], [10.0, 7.9375, 0]],
        [[0.0625, 0.125, 0], [0.0625, 7.875, 0]],
        [[9.9375, 0.125, 0], [9.9375, 7.875, 0]],
    ]
    assert [piece["original_span_ft"] for piece in pieces if "original_span_ft" in piece] == [[0, 8], [0, 8]]


def test_wall_finish_rejects_outline_hole_inside_the_full_width(run):
    # The hole is away from the footprint center and corners.
    outer = loop(rectangle(0, 0, 10, 8), source=[50, 51, 52, 53])
    hole = loop(rectangle(2, 0.02, 3, 0.05), source=-1)
    room = Room(Seq([outer, hole]))
    report = run({1: room, 50: Wall(50), 51: Wall(51), 52: Wall(52), 53: Wall(53),
                  101: Level(), 102: Phase(), 103: WallType()}, ["1"], wall_type_id="103")
    first = report["rooms"][0]["boundary_loops"][0][0]["wall_finish"]
    assert first["status"] == "incomplete"
    assert first["finish_footprints"] == [{
        "status": "excluded", "span_ft": [0.0, 10.0],
        "reason": "finish_width_does_not_fit_on_one_side",
    }]


def test_wall_finish_projects_all_box_corners_on_a_diagonal_boundary(run):
    wall = Wall(50, [151])
    room = Room(Seq([loop([(0, 10, 0), (10, 0, 0), (10, 10, 0)], source=[50, 51, 52])]))
    door = Door(151, wall, (4, 4, 0), (6, 6, 7))
    report = run({1: room, 50: wall, 51: Wall(51), 52: Wall(52),
                  101: Level(), 102: Phase(), 103: WallType(), 151: door}, ["1"], wall_type_id="103")
    first = report["rooms"][0]["boundary_loops"][0][0]["wall_finish"]
    assert first["boundary_spans_ft"][0] == pytest.approx([0.0, 8 / 2**0.5])
    assert first["boundary_spans_ft"][1] == pytest.approx([12 / 2**0.5, 200**0.5])


@pytest.mark.parametrize("control,reason", [
    ("end", "door_crosses_or_touches_boundary_end"),
    ("unknown", "wall_insert_requires_review"),
    ("other_host", "wall_insert_requires_review"),
])
def test_wall_finish_excludes_unverified_insert_without_hiding_horizontal_preview(run, control, reason):
    wall = Wall(50, [151])
    door = Door(151, Wall(999) if control == "other_host" else wall,
                (-0.1 if control == "end" else 2, -0.2, 0), (4, 0.2, 7))
    if control == "unknown":
        door.Category = NS(Id=NS(Value=-99))
    room = Room(Seq([loop(rectangle(0, 0, 10, 8), source=[50, 51, 52, 53])]))
    report = run({1: room, 50: wall, 101: Level(), 102: Phase(), 103: WallType(), 104: FloorType(),
                  151: door}, ["1"], wall_type_id="103",
                 horizontal_finishes={"floor": {"type_id": "104", "offset_ft": 0.0}})
    row, = report["rooms"]
    assert report["status"] == "incomplete" and row["wall_finish"]["status"] == "incomplete"
    assert row["horizontal_finishes"][0]["kind"] == "floor"
    assert row["boundary_loops"][0][0]["wall_finish"] == {"status": "excluded", "reason": reason}
