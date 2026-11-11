"""Exercise the script entry point with a small offline Revit stand-in."""

from pathlib import Path
from types import SimpleNamespace as NS
import runpy
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "place-floor-drains.py"


class Id:
    def __init__(self, value):
        self.Value = value

    def __eq__(self, other):
        return isinstance(other, Id) and self.Value == other.Value


class XYZ:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class LocationPoint:
    def __init__(self, point):
        self.Point = point


class FamilyInstance:
    def __init__(self, symbol, level_id):
        self.Id = Id(9)
        self.Symbol = symbol
        self.LevelId = level_id
        self.Location = LocationPoint(XYZ(4, 4, 0))


class Room:
    Id, Number, Area, LevelId = Id(42), "A", 64, Id(1)

    def get_Parameter(self, _):
        return NS(AsString=lambda: "TOILET")

    def get_BoundingBox(self, _):
        return NS(Min=XYZ(0, 0, 0), Max=XYZ(8, 8, 8))

    def GetBoundarySegments(self, _):
        # The stand-in has four straight walls, and can model a nearby internal boundary.
        distances = [lambda p: p.X, lambda p: 8 - p.X, lambda p: p.Y, lambda p: 8 - p.Y]
        if hasattr(self, "interior_boundary"):
            distances.append(lambda p: abs(p.X - self.interior_boundary))
        curves = [NS(GetEndPoint=lambda _: XYZ(0, 0, 0), Distance=distance) for distance in distances]
        return [[NS(GetCurve=lambda c=c: c) for c in curves]]

    def IsPointInRoom(self, point):
        return 0 < point.X < 8 and 0 < point.Y < 8 and 0 < point.Z < 8


@pytest.fixture
def run(monkeypatch):
    symbol = NS(Id=Id(3), Family=NS(Name="PLBG-FLOOR DRAIN-2D"), Name="Floor Drain-2D", IsActive=True)
    items = {"rooms": [Room()], "fixtures": [], "floor": [], "elements": {}}
    edits = []
    level = NS(Id=Id(1), Name="Test level", Elevation=0)
    view = NS(Id=Id(10), GenLevel=level, get_Parameter=lambda _: NS(AsElementId=lambda: Id(11)))

    class Collector:
        def __init__(self, doc, view_id=None):
            self.category = None

        def OfClass(self, cls):
            return [symbol] if cls == "symbol" else items.get(cls, [])

        def OfCategory(self, category):
            self.category = category
            return self if category in ("fixtures", "doors") else items.get(category, [])

        def WhereElementIsNotElementType(self):
            return items.get(self.category, [])

    class Transaction:
        def __init__(self, doc, name):
            edits.append(name)

        def Start(self):
            pass

        def Commit(self):
            pass

        def RollBack(self):
            pass

    def create(point, sym, level, _):
        fixture = FamilyInstance(sym, level.Id)
        fixture.Location.Point = point
        fixture.get_Parameter = lambda _: None
        items["fixtures"].append(fixture)
        return fixture

    def get_element(element_id):
        if element_id.Value == 10:
            return view
        return items["elements"].get(element_id.Value) or next(
            (f for f in items["fixtures"] if f.Id == element_id), None
        )

    db = NS(
        ElementId=Id, XYZ=XYZ, LocationPoint=LocationPoint, FamilyInstance=FamilyInstance,
        FamilySymbol="symbol", Floor="floor", FilteredElementCollector=Collector, Transaction=Transaction,
        SpatialElementBoundaryOptions=object,
        BuiltInParameter=NS(VIEW_PHASE="phase", ROOM_NAME="room_name", INSTANCE_ELEVATION_PARAM=1,
                            INSTANCE_FREE_HOST_OFFSET_PARAM=2, DOOR_WIDTH="door_width"),
        BuiltInCategory=NS(OST_PlumbingFixtures="fixtures", OST_Doors="doors", OST_Rooms="rooms"),
        Element=NS(Name=NS(GetValue=lambda element: element.Name)),
        Structure=NS(StructuralType=NS(NonStructural=0)),
        HostObjectUtils=NS(GetTopFaces=lambda _: ["top"]),
        FillPatternTarget=NS(Model="model"),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    doc = NS(GetElement=get_element, Regenerate=lambda: None, Create=NS(NewFamilyInstance=create))

    def execute(**inputs):
        return runpy.run_path(str(SCRIPT), init_globals={"inputs": dict(inputs, view_id=10), "doc": doc})["result"]

    return execute, edits, items, symbol


def test_explicit_point_does_not_duplicate_an_existing_drain(run):
    execute, edits, items, symbol = run
    items["fixtures"].append(FamilyInstance(symbol, Id(1)))
    result = execute(points={"42": [4, 4]})
    assert result["created"] == {}
    assert result["rooms"][0]["skip"] == "already has a drain"
    assert edits == []


def test_explicit_point_requires_membership_in_selected_room(run):
    execute, edits, _, _ = run
    result = execute(points={"42": [20, 20]})
    assert result["created"] == {}
    assert result["ASK_USER"][0]["room_id"] == 42
    assert edits == []


def test_explicit_point_creates_one_drain_on_target_level(run):
    execute, edits, _, _ = run
    result = execute(points={"42": [4, 4]})
    assert result["created"] == {"42": 9}
    assert result["z_above_level_should_be_0"] == {"42": 0}
    assert edits == ["Place floor drains"]


def test_resnap_requires_drain_ids_from_approved_placement(run):
    execute, edits, items, symbol = run
    items["fixtures"].append(FamilyInstance(symbol, Id(1)))
    with pytest.raises(ValueError, match="resnap_drain_ids"):
        execute(resnap=True)
    assert edits == []


def test_point_inside_bounding_box_can_fail_internal_boundary_clearance(run):
    execute, edits, items, _ = run
    items["rooms"][0].interior_boundary = 4.5
    result = execute(points={"42": [4, 4]})
    assert result["created"] == {}
    assert result["ASK_USER"][0]["room_id"] == 42
    assert edits == []


def test_drain_on_other_level_does_not_block_approved_point(run):
    execute, edits, items, symbol = run
    old = FamilyInstance(symbol, Id(2))
    old.Id = Id(8)
    items["fixtures"].append(old)
    assert execute(points={"42": [4, 4]})["created"] == {"42": 9}
    assert edits == ["Place floor drains"]


def _wc(y):
    wc = FamilyInstance(NS(Id=Id(4), Family=NS(Name="Water Closet"), Name="WC"), Id(1))
    wc.Id = Id(8)
    wc.Location.Point = XYZ(0, y, 0)
    wc.get_BoundingBox = lambda _: NS(Min=XYZ(0, y - 0.6, 0), Max=XYZ(2, y + 0.6, 2))
    return wc


def test_corner_mode_places_drain_between_wc_and_nearer_side_wall(run):
    execute, edits, items, _ = run
    items["fixtures"] = [_wc(1.5)]
    result = execute()
    assert result["created"] == {"42": 9}
    row = result["rooms"][0]
    assert row["mode"] == "corner"
    assert row["xy"][0] == 0.5
    assert 0.4 < row["xy"][1] < 0.5
    assert edits == ["Place floor drains"]


def test_corner_mode_asks_when_wc_is_far_from_both_side_walls(run):
    execute, edits, items, _ = run
    items["fixtures"] = [_wc(4)]
    result = execute()
    assert result["created"] == {}
    assert result["ASK_USER"][0]["room_id"] == 42
    assert edits == []


def _shower():
    shower = FamilyInstance(NS(Id=Id(5), Family=NS(Name="Shower"), Name="3x3"), Id(1))
    shower.Id = Id(7)
    return shower


def test_room_with_shower_is_not_placed_by_rule(run):
    execute, edits, items, _ = run
    items["fixtures"] = [_wc(1.5), _shower()]
    result = execute()
    assert result["created"] == {}
    assert result["ASK_USER"][0]["room_id"] == 42
    assert edits == []


def test_approved_point_places_drain_in_room_with_shower(run):
    execute, edits, items, _ = run
    items["fixtures"] = [_wc(1.5), _shower()]
    result = execute(points={"42": [2, 2]})
    assert result["created"] == {"42": 9}
    assert result["rooms"][0]["mode"] == "user point"
    assert edits == ["Place floor drains"]


def test_resnap_with_unmatched_id_returns_actionable_result(run):
    execute, edits, _, _ = run
    result = execute(resnap=True, resnap_drain_ids=[9])
    assert result["moved"] == {}
    assert result["ASK_USER"][-1]["drain_id"] == 9
    assert edits == []


def test_resnap_moves_only_approved_drain_to_measured_grid(run):
    from math import pi

    execute, edits, items, symbol = run
    drain = FamilyInstance(symbol, Id(1))
    moves = []
    drain.Location.Move = lambda offset: moves.append((offset.X, offset.Y, offset.Z)) or True
    wc = FamilyInstance(NS(Id=Id(4), Family=NS(Name="Water Closet"), Name="WC"), Id(1))
    wc.Id = Id(8)
    wc.Location.Point = XYZ(0, 4, 0)
    wc.get_BoundingBox = lambda _: NS(Min=XYZ(0, 3.5, 0), Max=XYZ(2, 4.5, 2))
    items["fixtures"] = [drain, wc]
    items["floor"] = [NS(
        Id=Id(15), GetTypeId=lambda: Id(16),
        get_BoundingBox=lambda _: NS(Min=XYZ(0, 0, -0.5), Max=XYZ(8, 8, 0)),
        GetGeometryObjectFromReference=lambda _: NS(Project=lambda point: NS(XYZPoint=point)),
    )]
    grid = [NS(Angle=angle, Offset=1.0, GetSegments=lambda: []) for angle in (0.0, pi / 2)]
    layer = NS(MaterialId=Id(17))
    items["elements"] = {
        16: NS(Name="Tile", GetCompoundStructure=lambda: NS(LayerCount=1, GetLayers=lambda: [layer])),
        17: NS(SurfaceForegroundPatternId=Id(18)),
        18: NS(Name="Grid", GetFillPattern=lambda: NS(Target="model", GetFillGrids=lambda: grid)),
    }

    unmeasured = execute(resnap=True, resnap_drain_ids=[9])
    assert unmeasured["moved"] == {}
    assert unmeasured["NEEDS_GRID_MEASURE"] == {"15": ["A"]}
    assert edits == []

    measured = execute(resnap=True, resnap_drain_ids=[9], grid_offsets={"15": [0.5, 0.0]})
    assert measured["moved"] == {"42": 9}
    assert moves == [(-0.5, 0.0, 0)]
    assert edits == ["Snap floor drains to tile grid"]


def test_corner_respects_an_internal_room_boundary(run):
    execute, edits, items, _ = run
    items["fixtures"] = [_wc(1.5)]
    items["rooms"][0].interior_boundary = 0.51
    result = execute()
    assert result["created"] == {}
    assert "boundary" in result["ASK_USER"][0]["skip"]
    assert edits == []


def test_corner_uses_the_fixture_edge_on_the_selected_side(run):
    execute, edits, items, _ = run
    wc = _wc(1.5)
    wc.get_BoundingBox = lambda _: NS(Min=XYZ(0, 0.25, 0), Max=XYZ(2, 1.85, 2))
    items["fixtures"] = [wc]
    result = execute()
    assert result["created"] == {}
    assert "gap" in result["ASK_USER"][0]["skip"]
    assert edits == []


def test_corner_respects_door_swing(run):
    execute, edits, items, _ = run
    room = items["rooms"][0]
    door_symbol = NS(get_Parameter=lambda _: NS(AsDouble=lambda: 3.0))
    door = FamilyInstance(door_symbol, room.LevelId)
    door.Location.Point = XYZ(0, 1, 0)
    door.HandOrientation = XYZ(0, 1, 0)
    door.get_ToRoom = lambda _: room
    door.get_FromRoom = lambda _: None
    items["doors"] = [door]
    items["fixtures"] = [_wc(1.5)]
    result = execute()
    assert result["created"] == {}
    assert "door swing" in result["ASK_USER"][0]["skip"]
    assert edits == []
