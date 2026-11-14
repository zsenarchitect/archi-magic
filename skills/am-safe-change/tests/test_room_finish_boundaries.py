import json
import importlib
import hashlib
from math import dist
from pathlib import Path
import runpy
import subprocess
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

    def Add(self, other):
        return Point(self.X + other.X, self.Y + other.Y, self.Z + other.Z)

    def Multiply(self, scalar):
        return Point(self.X * scalar, self.Y * scalar, self.Z * scalar)

    def GetLength(self):
        return self.DistanceTo(Point(0, 0, 0))


class Curve:
    IsBound = True

    def __init__(self, a, b):
        self.ends = Point(*a), Point(*b)

    def GetEndPoint(self, index):
        return self.ends[index]

    def GetType(self):
        return NS(Name="Line")

    @staticmethod
    def CreateBound(a, b):
        return Curve((a.X, a.Y, a.Z), (b.X, b.Y, b.Z))

    def Dispose(self):
        pass


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

    def GetCompoundStructure(self):
        return NS(GetWidth=lambda: 0.25, Dispose=lambda: None)


class CeilingType:
    UniqueId = "synthetic-ceiling-type"
    GetCompoundStructure = FloorType.GetCompoundStructure


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
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    db = NS(
        Architecture=NS(Room=Room), CurveLoop=CurveLoop, ElementId=int,
        SpatialElementBoundaryOptions=lambda: NS(Dispose=lambda: None),
        SpatialElementBoundaryLocation=NS(Finish="finish"),
        BoundaryValidation=NS(IsValidHorizontalBoundary=lambda profiles: bool(profiles)),
        Level=Level, Phase=Phase, FloorType=FloorType, CeilingType=CeilingType,
        BuiltInParameter=NS(ROOM_PHASE_ID="room-phase"), StorageType=NS(ElementId="element-id", String="text"),
        Wall=Wall, WallType=WallType, FamilyInstance=Door, WallKind=NS(Basic="basic"),
        WallCrossSection=NS(Vertical="vertical"), BuiltInCategory=NS(OST_Doors=-42),
        ElementOnPhaseStatus=NS(New="new", Existing="existing"),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int, Guid=str, String=str))
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


def test_room_finish_rules_map_each_room_and_exclude_unresolved_values(run):
    def room_with(*parameters):
        room = Room(Seq([loop(rectangle(0, 0, 8, 8))]))

        def get(name):
            assert name == "Finish Code"
            return list(parameters)

        room.GetParameters = get
        return room

    def text_param(value):
        return NS(StorageType="text", HasValue=True, Id=NS(Value=305), AsString=lambda: value)

    rooms = {
        1: room_with(text_param(" A ")), 2: room_with(text_param("B")), 3: room_with(text_param("a")),
        4: room_with(text_param("A"), text_param("B")), 5: room_with(), 6: room_with(NS(StorageType="number")),
    }
    rules = {"parameter": "Finish Code", "by_value": {
        "A": {"horizontal_finishes": {"floor": {"type_id": "103", "offset_ft": 0}}},
        "B": {"horizontal_finishes": {"ceiling": {"type_id": "104", "offset_ft": 9}}},
    }}
    rooms.update({101: Level(), 102: Phase(), 103: FloorType(), 104: CeilingType()})
    report = run(rooms, ["1", "2", "3", "4", "5", "6"], finish_rules=rules)
    first, second, *excluded = report["rooms"]
    assert report["status"] == "incomplete"
    assert first["finish_rule"] == {"parameter": "Finish Code", "parameter_id": "305", "value": "A"}
    assert first["horizontal_finishes"][0]["kind"] == "floor"
    assert second["finish_rule"]["value"] == "B"
    assert second["horizontal_finishes"][0]["kind"] == "ceiling"
    assert [item["reason"] for item in excluded] == [
        "finish_parameter_value_unmapped", "finish_parameter_ambiguous", "finish_parameter_missing",
        "finish_parameter_value_unavailable",
    ]
    with pytest.raises(ValueError, match="Choose direct finish types"):
        run({}, ["1"], finish_rules=rules, horizontal_finishes=rules["by_value"]["A"]["horizontal_finishes"])


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


@pytest.mark.parametrize("state", [
    "empty", "match", "partial", "copied", "edited", "wrong_geometry", "nearby", "nearby_unavailable",
])
def test_existing_finish_audit_reconciles_exact_set_and_native_sketch(run, monkeypatch, state):
    db = sys.modules["pyrevit.api"].DB
    db.StorageType.Double = "double"
    db.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM = "floor-offset"
    db.BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM = "ceiling-offset"
    tagged = []
    field = NS(FieldName="Payload", ValueType=NS(FullName="System.String"), ContainerType="simple")
    schema = NS(SchemaName="ArchiMagicRoomFinishV1", ListFields=lambda: [field])
    ext = NS(ContainerType=NS(Simple="simple"), Schema=NS(Lookup=lambda _guid: schema),
             ExtensibleStorageFilter=lambda guid: guid)
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.DB.ExtensibleStorage", ext)
    monkeypatch.delitem(sys.modules, "room_finish_provenance", raising=False)
    provenance = importlib.import_module("room_finish_provenance")

    class Floor:
        DesignOption = None
        LevelId = NS(Value=101)
        CreatedPhaseId = NS(Value=102)
        DemolishedPhaseId = NS(Value=-1)

        def __init__(self, value, type_id, offset, curves):
            self.Id = NS(Value=value)
            self.UniqueId = "finish-" + str(value)
            self.type_id, self.offset = type_id, offset
            self.SketchId = NS(Value=value + 10)
            self.sketch = db.Sketch()
            self.sketch.OwnerId = self.Id
            self.sketch.Profile = [curves]

        def GetTypeId(self):
            return self.type_id

        def get_Parameter(self, _name):
            return NS(StorageType="double", HasValue=True, AsDouble=lambda: self.offset)

        def GetEntity(self, actual_schema):
            assert actual_schema is schema
            return NS(IsValid=lambda: True, Get={str: lambda _field: json.dumps(self.record)})

    db.Floor = Floor
    db.Ceiling = type("Ceiling", (Floor,), {})
    db.Sketch = type("Sketch", (), {})
    db.Wall = Wall
    db.XYZ = Point
    db.Outline = lambda low, high: NS(low=low, high=high, Dispose=lambda: None)
    db.BoundingBoxIntersectsFilter = lambda box, tolerance: NS(
        box=box, tolerance=tolerance, Dispose=lambda: None,
    )
    manual_floor = NS(Id=NS(Value=203), UniqueId="manual-floor", GetTypeId=lambda: NS(Value=103))

    def near(class_, filter_):
        low, high = filter_.box.low, filter_.box.high
        z = 25.0 if class_ is Floor else 34.0
        assert (low.X, low.Y, low.Z, high.X, high.Y, high.Z) == (0, 0, z - 0.25, 8, 8, z + 0.25)
        assert filter_.tolerance == -1e-6
        if state == "nearby_unavailable":
            raise RuntimeError("fixture collector failed")
        if class_ is Floor:
            return [floor, manual_floor] if state == "nearby" else ([floor] if floor in tagged else [])
        return [ceiling] if ceiling in tagged else []

    db.FilteredElementCollector = lambda _doc: NS(
        WherePasses=lambda _filter: tagged,
        OfClass=lambda class_: NS(WherePasses=lambda filter_: near(class_, filter_)),
        Dispose=lambda: None,
    )

    boundary = loop(rectangle(0, 0, 8, 8))
    room = Room(Seq([boundary]))
    models = {1: room, 101: Level(), 102: Phase(), 103: FloorType(), 104: CeilingType()}
    horizontal = {"floor": {"type_id": "103", "offset_ft": 0},
                  "ceiling": {"type_id": "104", "offset_ft": 9}}
    preview = run(models, ["1"], horizontal_finishes=horizontal)["rooms"][0]
    expected = provenance.expected_pieces(preview)
    curves = [segment.GetCurve() for segment in boundary]
    floor, ceiling = Floor(201, 103, 0, curves), db.Ceiling(202, 104, 9, curves)
    models.update({211: floor.sketch, 212: ceiling.sketch})
    doc = NS(GetElement=lambda item: models.get(getattr(item, "Value", item)))
    if state == "wrong_geometry":
        ceiling.sketch.Profile = [[segment.GetCurve() for segment in loop(rectangle(1, 0, 8, 8))]]
    for key, piece in expected.items():
        element = floor if piece["kind"] == "floor" else ceiling
        observed = provenance.observed_piece(doc, element, piece["kind"])
        element.record = dict({field: piece[field] for field in ("kind", "type_uid", "level_uid")},
                              room_uid=room.UniqueId, element_uid=element.UniqueId, piece_key=key,
                              observed_sha256=provenance.digest(observed))
    if state == "copied":
        floor.UniqueId = "copied-floor"
    if state == "edited":
        floor.offset = 0.5
    tagged.extend([] if state == "empty" else [floor] if state == "partial" else [floor, ceiling])
    report = run(models, ["1"], horizontal_finishes=horizontal, audit_existing_finishes=True)
    if state == "empty":
        # The fixture's room boundary has no resolvable host walls.
        models[105] = WallType()
        mixed = run(models, ["1"], horizontal_finishes=horizontal, audit_existing_finishes=True,
                    wall_type_id="105", wall_dimensions={"height_ft": 9, "base_offset_ft": 0})
        row, = mixed["rooms"]
        assert mixed["status"] == "incomplete" and row["wall_finish"]["status"] == "incomplete"
        assert row["existing_finishes"]["status"] == "no_tagged_pieces"
        assert row["existing_finishes"]["expected_piece_keys"] == sorted(expected)
        assert row["existing_finishes"]["nearby"] == {"status": "none_in_scope", "candidates": []}
    audit = report["rooms"][0]["existing_finishes"]
    assert audit["expected_piece_keys"] == sorted(expected)
    if state in ("empty", "match"):
        assert report["status"] == "reported"
        assert audit["status"] == ("no_tagged_pieces" if state == "empty" else "tagged_set_matches")
        assert audit["nearby"] == {"status": "none_in_scope", "candidates": []}
    elif state in ("nearby", "nearby_unavailable"):
        assert report["status"] == "incomplete" and audit["status"] == "tagged_set_matches"
        if state == "nearby":
            assert audit["nearby"] == {"status": "review_required", "candidates": [
                {"id": "203", "uid": "manual-floor", "kind": "floor", "type_id": "103", "same_selected_type": True},
            ]}
        else:
            assert audit["nearby"] == {"status": "unavailable", "reason": "RuntimeError"}
    else:
        assert report["status"] == "incomplete" and audit["status"] == "conflict"
        assert audit["reason"] == ("tagged_piece_set_differs" if state == "partial"
                                    else "tagged_piece_changed_or_copied")


@pytest.mark.parametrize("with_dimensions", [False, True])
def test_existing_wall_scan_uses_each_trimmed_footprint_and_deduplicates_candidates(run, monkeypatch, with_dimensions):
    db = sys.modules["pyrevit.api"].DB
    db.Floor, db.Ceiling = type("Floor", (), {}), type("Ceiling", (), {})
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.DB.ExtensibleStorage", NS(
        ContainerType=NS(Simple="simple"), ExtensibleStorageFilter=str, Schema=NS(Lookup=lambda _guid: None),
    ))
    monkeypatch.delitem(sys.modules, "room_finish_provenance", raising=False)
    db.XYZ = Point
    db.Outline = lambda low, high: NS(low=low, high=high, Dispose=lambda: None)
    db.BoundingBoxIntersectsFilter = lambda box, _tolerance: box
    seen = []
    manual = NS(Id=NS(Value=201), UniqueId="manual-wall", GetTypeId=lambda: NS(Value=103))

    def near(box):
        low, high = box.low, box.high
        seen.append((low.X, low.Y, low.Z, high.X, high.Y, high.Z))
        return [manual]

    db.FilteredElementCollector = lambda _doc: NS(
        OfClass=lambda _class: NS(WherePasses=near), Dispose=lambda: None,
    )
    room = Room(Seq([loop(rectangle(0, 0, 10, 8), source=[50, 51, 52, 53])]))
    room.get_BoundingBox = lambda _view: NS(Enabled=True, Transform=NS(IsIdentity=True),
                                           Min=Point(0, 0, 24.75), Max=Point(10, 8, 35))
    dimensions = {"height_ft": 9, "base_offset_ft": 0.25} if with_dimensions else None
    report = run({1: room, 50: Wall(50), 51: Wall(51), 52: Wall(52), 53: Wall(53),
                  101: Level(), 102: Phase(), 103: WallType()}, ["1"],
                 wall_type_id="103", audit_existing_finishes=True, wall_dimensions=dimensions)
    assert report["status"] == "incomplete"
    low, high = (25.25, 34.25) if with_dimensions else (24.75, 35)
    assert set(seen) == {
        (0, 0, low, 10, 0.125, high), (0, 7.875, low, 10, 8, high),
        (0, 0.125, low, 0.125, 7.875, high), (9.875, 0.125, low, 10, 7.875, high),
    }
    if with_dimensions:
        provenance = sys.modules["room_finish_provenance"]
        assert {(piece["base_offset_ft"], piece["height_ft"])
                for piece in provenance.expected_pieces(report["rooms"][0]).values()} == {(0.25, 9.0)}
    assert report["rooms"][0]["existing_finishes"]["nearby"]["candidates"] == [
        {"id": "201", "uid": "manual-wall", "kind": "wall", "type_id": "103", "same_selected_type": True},
    ]


class Element:
    def __init__(self, number, **values):
        self.Id = NS(Value=number)
        self.UniqueId = "fixture-%s" % number
        self.VersionGuid = "fixture-version"
        self.__dict__.update(values)

    def get_Parameter(self, parameter):
        element_id = self.parameters.get(parameter)
        return NS(StorageType="id", AsElementId=lambda: element_id)


class View(Element):
    DesignOption = None
    IsTemplate = False


class ViewPlan(View):
    ViewType = "floor plan"


class ViewPhase(Element):
    pass


class PlanarFace:
    def __init__(self, vertices, normal_z, source=None):
        self.vertices = vertices
        self.FaceNormal = NS(Z=normal_z)
        self.source = source

    def GetEdgesAsCurveLoops(self):
        curves = CurveLoop(Curve(a, b) for a, b in zip(self.vertices, self.vertices[1:] + self.vertices[:1]))
        return Seq([curves])


class Collector:
    def __init__(self, document):
        self.document = document

    def OfClass(self, cls):
        self.elements = self.document.collected.get(cls, [])
        return self

    def GetElementCount(self):
        return len(self.elements)

    def __iter__(self):
        return iter(self.elements)

    def Dispose(self):
        pass


@pytest.fixture
def snapshot_query(monkeypatch):
    bottom = rectangle(0, 0, 8, 6, z=2)
    top = rectangle(0, 0, 8, 6, z=11)
    segments = loop(rectangle(0, 0, 8, 6, z=0))
    for index, segment in enumerate(segments):
        segment.ElementId.Value = 50 + index
    room = Room(Seq([segments]))
    room.Id = NS(Value=9007199254740993)
    room.Number = "101"
    room.VersionGuid = "room-version"
    room.DesignOption = None
    room.IsPointInRoom = lambda point: 0 < point.X < 8 and 0 < point.Y < 6 and 2 < point.Z < 11
    room.get_Parameter = lambda _: NS(StorageType="id", AsElementId=lambda: NS(Value=8))
    level = Element(101)
    host = ViewPlan(2, GenLevel=level, Name="Fixture Floor Plan", parameters={"view phase": NS(Value=8)})
    design_option = type("DesignOption", (), {})
    view_family_type = type("ViewFamilyType", (Element,), {})
    template = View(4, IsTemplate=True, ViewType="elevation", Name="Fixture Elevation Template")
    elements = {
        101: level, 2: host, 3: view_family_type(3, ViewFamily="elevation"),
        4: template, 8: ViewPhase(8), 9: ViewPhase(9), room.Id.Value: room,
        10: view_family_type(10, ViewFamily="section"),
        11: View(11, IsTemplate=True, ViewType="section", Name="Fixture Section Template"),
        **{50 + i: Element(50 + i) for i in range(4)},
    }
    faces = [PlanarFace(top, 1), PlanarFace(bottom, -1)]
    for i in range(4):
        j = (i + 1) % 4
        faces.append(PlanarFace([bottom[i], bottom[j], top[j], top[i]], 0, 50 + i))

    def subfaces(face):
        return Seq([NS(
            Valid=True, SubfaceType="side",
            SpatialBoundaryElement=NS(HostElementId=NS(Value=face.source), LinkedElementId=NS(Value=-1)),
            GetSubface=lambda: face, Dispose=lambda: None,
        )])

    def intersect(line, options):
        assert options.ResultType == "inside"
        a, b = line.ends
        segment_start, segment_end = 0.0, 1.0
        for start, end, lower, upper in ((a.X, b.X, 0, 8), (a.Y, b.Y, 0, 6), (a.Z, b.Z, 2, 11)):
            delta = end - start
            if delta:
                first, second = sorted(((lower - start) / delta, (upper - start) / delta))
                segment_start = max(segment_start, first)
                segment_end = min(segment_end, second)
        vector = Point(b.X - a.X, b.Y - a.Y, b.Z - a.Z)
        return NS(SegmentCount=1, GetCurveSegment=lambda _: Curve.CreateBound(
            a.Add(vector.Multiply(segment_start)), a.Add(vector.Multiply(segment_end)),
        ), Dispose=lambda: None)

    solid = NS(Faces=None, Volume=432, IntersectWithCurve=intersect)

    def calculate(_):
        solid.Faces = Faces(faces)
        return NS(GetGeometry=lambda: solid,
                  GetBoundaryFaceInfo=subfaces, Dispose=lambda: None)

    class Faces(list):
        Size = property(len)

    db = NS(
        Architecture=NS(Room=Room), CurveLoop=CurveLoop, ElementId=lambda value: NS(Value=value),
        SpatialElementBoundaryOptions=lambda: NS(Dispose=lambda: None),
        SpatialElementBoundaryLocation=NS(Finish="finish"),
        BoundaryValidation=NS(IsValidHorizontalBoundary=lambda profiles: bool(profiles)),
        View=View, ViewPlan=ViewPlan, Phase=ViewPhase, DesignOption=design_option, ViewFamilyType=view_family_type,
        ViewFamily=NS(Elevation="elevation", Section="section"),
        ViewType=NS(FloorPlan="floor plan", Elevation="elevation", Section="section"),
        StorageType=NS(ElementId="id"), BuiltInParameter=NS(ROOM_PHASE_ID="room phase", VIEW_PHASE="view phase"),
        RevitLinkInstance=type("RevitLinkInstance", (), {}), PlanarFace=PlanarFace, Line=Curve,
        XYZ=Point, SolidCurveIntersectionOptions=lambda: NS(Dispose=lambda: None),
        SolidCurveIntersectionMode=NS(CurveSegmentsInside="inside"),
        SubfaceType=NS(Side="side"), FilteredElementCollector=Collector,
        SpatialElementGeometryCalculator=lambda document, options: NS(
            CalculateSpatialElementGeometry=calculate, Dispose=lambda: None,
        ),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=ListFactory()))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0)),
    ))
    document = NS(
        IsFamilyDocument=False, PathName="fixture.rvt", ProjectInformation=Element(1),
        collected={View: [host, template]}, GetElement=lambda element_id: elements.get(element_id.Value),
    )
    selection = {
        "padding_feet": 0.25,
        "rooms": [{
            "room_id": str(room.Id.Value), "host_plan_id": "2",
            "families": {"elevation": {"type_id": "3", "template_id": "4"}},
            "requests": [{"kind": "elevation", "side": 0}],
        }],
    }

    def execute(query_inputs=None):
        return runpy.run_path(str(SCRIPT), init_globals={
            "doc": document, "inputs": query_inputs or {"room_views": selection},
        })["result"]

    return NS(execute=execute, faces=faces, host=host, segments=segments, template=template,
              room=room, solid=solid, selection=selection)


def test_snapshot_native_finish_evidence_flows_to_preview(snapshot_query, tmp_path):
    result = snapshot_query.execute()
    assert result["status"] == "reported", result
    snapshot = json.loads(result["payload"])
    assert snapshot["view_names"] == ["Fixture Elevation Template", "Fixture Floor Plan"]
    room, = snapshot["rooms"]
    assert room["id"] == "9007199254740993"
    assert room["heights"] == [2, 11]
    assert [source["id"] for source in room["finish_sources"]] == ["50", "51", "52", "53"]
    assert "fixture.rvt" not in result["payload"]
    source = tmp_path / "query.json"
    source.write_text(json.dumps({"result": result}), encoding="utf-8")
    output = tmp_path / "preview.json"
    run = subprocess.run([
        sys.executable, str(SCRIPT.with_name("room_views_preview.py")),
        "--snapshot", str(source), "--out", str(output),
    ], capture_output=True, check=False)
    assert run.returncode == 0, run.stderr
    preview = json.loads(output.read_text(encoding="utf-8"))
    assert preview["steps"] == []
    view, = preview["proposal"]["views"]
    assert view["origin"] == [4, 3, 6.5]
    assert view["crop_max"] == [4.25, 4.75]
    assert view["source"]["finish_source"]["uid"] == "fixture-50"


@pytest.mark.parametrize(("change", "reason"), [
    (lambda fixture: setattr(fixture.faces[2], "source", 51), "native finish source"),
    (lambda fixture: fixture.faces[2].vertices.__setitem__(2, (8, 0, 10)), "intermediate height"),
    (lambda fixture: fixture.host.parameters.update({"view phase": NS(Value=9)}), "same phase"),
    (lambda fixture: setattr(fixture.segments[0].LinkElementId, "Value", 71), "native sources"),
    (lambda fixture: setattr(fixture.template, "IsTemplate", False), "view template"),
])
def test_native_reader_blocks_incomplete_or_inconsistent_evidence(snapshot_query, change, reason):
    change(snapshot_query)
    result = snapshot_query.execute()
    assert result["status"] == "blocked", result
    assert reason in result["reason"]
    assert "payload" not in result


@pytest.fixture
def preflight_inputs(snapshot_query, tmp_path):
    room = snapshot_query.selection["rooms"][0]
    room["families"]["section"] = {"type_id": "10", "template_id": "11"}
    room["requests"].append({"kind": "section", "cut": [[2, -1], [2, 7]], "look": [1, 0]})
    report = snapshot_query.execute()
    assert report["status"] == "reported", report
    source = tmp_path / "query.json"
    source.write_text(json.dumps({"result": report}), encoding="utf-8")
    output = tmp_path / "preview.json"
    preflight = tmp_path / "preflight.json"
    run = subprocess.run([
        sys.executable, str(SCRIPT.with_name("room_views_preview.py")), "--snapshot", str(source),
        "--out", str(output), "--preflight-out", str(preflight),
    ], capture_output=True, check=False)
    assert run.returncode == 0, run.stderr
    return json.loads(preflight.read_text(encoding="utf-8"))


def test_native_preflight_measures_first_hit_and_section_slice(snapshot_query, preflight_inputs):
    result = snapshot_query.execute(preflight_inputs)
    assert result["status"] == "verified", result
    assert result["placement_available"] is False
    assert result["proposal_sha256"] == preflight_inputs["room_view_preflight"]["proposal_sha256"]
    elevation, section = result["checks"]
    assert elevation["first_hit"] == pytest.approx([4, 0, 6.5])
    assert section["slice_start"] == pytest.approx([2, 6, 6.5])
    assert section["slice_end"] == pytest.approx([2, 0, 6.5])


def test_native_preflight_rejects_stale_snapshot(snapshot_query, preflight_inputs):
    snapshot_query.host.Name = "Changed Name"
    result = snapshot_query.execute(preflight_inputs)
    assert result["status"] == "blocked"
    assert "evidence has changed" in result["reason"]


def test_native_preflight_requires_marker_to_be_in_room(snapshot_query, preflight_inputs):
    snapshot_query.room.IsPointInRoom = lambda _: False
    result = snapshot_query.execute(preflight_inputs)
    assert result["status"] == "blocked"
    assert "outside the native room" in result["reason"]


def test_native_preflight_rejects_earlier_native_exit(snapshot_query, preflight_inputs):
    snapshot_query.solid.IntersectWithCurve = lambda line, _: NS(
        SegmentCount=1, GetCurveSegment=lambda index: Curve.CreateBound(line.ends[0], Point(4, 1, 6.5)),
        Dispose=lambda: None,
    )
    result = snapshot_query.execute(preflight_inputs)
    assert result["status"] == "blocked"
    assert "different native room boundary first" in result["reason"]


def test_native_preflight_rejects_a_reversed_direction_even_with_matching_digest(snapshot_query, preflight_inputs):
    evidence = preflight_inputs["room_view_preflight"]
    proposal = json.loads(evidence["proposal_json"])
    proposal["views"][1]["look"] = [-1, 0, 0]
    proposal["views"][1]["right"] = [0, 1, 0]
    evidence["proposal_json"] = json.dumps(proposal)
    result = snapshot_query.execute(preflight_inputs)
    assert result["status"] == "blocked"
    assert "proposal digest" in result["reason"]
    evidence["proposal_sha256"] = hashlib.sha256(evidence["proposal_json"].encode()).hexdigest()
    result = snapshot_query.execute(preflight_inputs)
    assert result["status"] == "blocked"
    assert "reviewed direction" in result["reason"]
