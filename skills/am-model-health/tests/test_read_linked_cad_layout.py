"""Exercise linked CAD geometry extraction with a simulated Revit geometry tree."""

import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import ModuleType, SimpleNamespace as Record

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def linked_dwg(monkeypatch):
    feet = lambda value: value / 304.8

    def point(x, y, z=0):
        return Record(X=feet(x), Y=feet(y), Z=feet(z))

    class Transform:
        def __init__(self, apply):
            self.OfPoint = apply

        def Multiply(self, other):
            return Transform(lambda vertex: self.OfPoint(other.OfPoint(vertex)))

    class Instance:
        def __init__(self, transform, items):
            self.Transform, self.items = transform, items

        def GetSymbolGeometry(self):
            return self.items

    class Ids(list):
        @property
        def Count(self):
            return len(self)

    def element_id(value):
        return Record(Value=int(value))

    class Line:
        def __init__(self, layer, start, end):
            self.GraphicsStyleId = element_id(layer)
            self.points = [point(*start), point(*end)]
            self.IsBound = True

        def GetEndPoint(self, index):
            return self.points[index]

    class Polyline:
        def __init__(self, layer, points):
            self.GraphicsStyleId = element_id(layer)
            self.points = Ids(point(*vertex) for vertex in points)

        def GetCoordinates(self):
            return self.points

    class Arc(Record):
        pass

    class Linked(Record):
        def GetTypeId(self):
            return element_id(3)

        def get_Geometry(self, options):
            assert options.ComputeReferences is False
            assert options.IncludeNonVisibleObjects is False
            return self.geometry

        def GetTotalTransform(self):
            raise AssertionError("The import transform would be applied twice.")

    class Level(Record):
        pass

    class Style(Record):
        pass

    class WallType(Record):
        pass

    class FamilySymbol(Record):
        def get_Parameter(self, parameter):
            assert parameter == "door-width"
            return self.width

    class Options(Record):
        def Dispose(self):
            pass

    mirror = Transform(lambda v: Record(X=-v.X, Y=v.Y + feet(250), Z=v.Z))
    rotate = Transform(lambda v: Record(X=feet(1000) - v.Y, Y=feet(2000) + v.X, Z=feet(3000) + v.Z))
    wall_a = Line(10, (0, 0), (4000, 0))
    wall_b = Line(10, (4000, 200), (0, 200))
    door = Line(11, (1500, 100), (2400, 100))
    boundary = Polyline(12, [(0, 0), (4000, 0), (4000, 2500), (0, 2500), (0, 0)])
    primitives = [wall_a, wall_b, door, boundary]
    link = Linked(IsLinked=True, UniqueId="aa-1", geometry=[Instance(rotate, [Instance(mirror, primitives)])])
    reference = Record(GetLinkedFileStatus=lambda: "Loaded", GetAbsolutePath=lambda: "drawing.dwg")
    objects = {
        1: link, 2: Level(UniqueId="aa-2", ProjectElevation=feet(3000)),
        3: Record(IsExternalFileReference=lambda: True, GetExternalFileReference=lambda: reference),
        80: WallType(UniqueId="wall-type", Kind="basic", Width=feet(200.25)),
        81: FamilySymbol(UniqueId="door-type", Category=Record(Id=element_id(-2000014)),
                         Family=Record(FamilyPlacementType="hosted"),
                         width=Record(StorageType="double", HasValue=True, AsDouble=lambda: feet(900))),
        **{id: Style(GraphicsStyleCategory=Record(Name=label)) for id, label in
           [(10, "WALL"), (11, "DOOR"), (12, "ROOM")]},
    }
    db = Record(ElementId=element_id, Transform=Record(Identity=Transform(lambda v: v)),
                GeometryInstance=Instance, Line=Line, PolyLine=Polyline, ImportInstance=Linked,
                Level=Level, GraphicsStyle=Style, Options=Options,
                WallType=WallType, WallKind=Record(Basic="basic"), FamilySymbol=FamilySymbol,
                BuiltInCategory=Record(OST_Doors=-2000014), BuiltInParameter=Record(DOOR_WIDTH="door-width"),
                FamilyPlacementType=Record(OneLevelBasedHosted="hosted"), StorageType=Record(Double="double"),
                ModelPathUtils=Record(ConvertModelPathToUserVisiblePath=lambda value: value))
    api, system = ModuleType("pyrevit.api"), ModuleType("System")
    api.DB, system.Int64 = db, int
    monkeypatch.setitem(sys.modules, "pyrevit.api", api)
    monkeypatch.setitem(sys.modules, "System", system)
    document = Record(IsFamilyDocument=False, PathName="/tmp/test-model.rvt",
                      ProjectInformation=Record(UniqueId="aa-3"), GetElement=lambda id: objects.get(id.Value))
    request = {"link_id": "1", "level_id": "2", "segment_layers": ["WALL", "DOOR"],
               "room_layers": ["ROOM"], "room_points_mm": {}}
    return document, request, primitives, Arc, element_id, reference


def read(document, request):
    return runpy.run_path(str(SCRIPTS / "read_linked_cad_layout.py"),
                          init_globals={"doc": document, "inputs": request})["result"]


def layout_mapping():
    return {"tolerance_mm": 1, "minimum_overlap_mm": 100, "door_setback_mm": 100,
            "walls": {"WALL": {"type_id": "80", "width_mm": 200}},
            "doors": {"DOOR": {"type_id": "81", "width_mm": 900}}, "room_layers": ["ROOM"]}


def test_nested_mirror_rotation_and_translation_feed_real_layout_cli(linked_dwg, tmp_path):
    document, request, *_ = linked_dwg
    discovered = read(document, request)
    assert discovered["status"] == "review_required"
    assert "verified_mapping_json" not in discovered
    candidate, = discovered["room_candidates"]
    assert candidate["vertices"][0] == pytest.approx([750, 2000, 3000])
    request["room_points_mm"] = {candidate["id"]: [-750, 500, 3000]}
    request["mapping"] = layout_mapping()
    result = read(document, request)
    assert result["status"] == "ok" and not result["unresolved"]
    snapshot = json.loads(result["snapshot_json"])
    assert snapshot["segments"][0]["end"] == pytest.approx([750, -2000, 3000])
    assert snapshot["segments"][1]["start"] == pytest.approx([550, -2000, 3000])
    assert snapshot["segments"][2]["start"] == pytest.approx([650, 500, 3000])
    assert snapshot["link_id"] == "aa-1" and snapshot["level_id"] == "aa-2"
    assert json.loads(result["verified_mapping_json"])["walls"]["WALL"]["width_mm"] == pytest.approx(200.25)
    assert request["mapping"]["walls"]["WALL"]["width_mm"] == 200
    assert [(row["layer"], row["type_uid"]) for row in result["type_evidence"]] == [
        ("WALL", "wall-type"), ("DOOR", "door-type")]
    geometry = tmp_path / "geometry.json"
    geometry.write_text(result["snapshot_json"])
    mapping = tmp_path / "mapping.json"
    mapping.write_text(result["verified_mapping_json"])
    report = json.loads(subprocess.check_output([sys.executable, SCRIPTS / "cad_layout_preview.py",
                                                geometry, "--mapping", mapping]))
    assert len(report["walls"]) == len(report["doors"]) == len(report["rooms"]) == 1
    assert not report["unresolved"]


@pytest.mark.parametrize("change,reason", [
    ("width", "differs from Revit"),
    ("wall_kind", "Basic Wall"),
    ("category", "level-and-host-based door"),
    ("placement", "level-and-host-based door"),
    ("missing_width", "no verifiable type width"),
    ("layers", "exactly the selected DWG layers"),
])
def test_refuses_unverified_native_mapping(linked_dwg, change, reason):
    document, request, _, _, id_value, _ = linked_dwg
    wall, door = (document.GetElement(id_value(identifier)) for identifier in (80, 81))
    request["mapping"] = layout_mapping()
    if change == "width":
        request["mapping"]["walls"]["WALL"]["width_mm"] = 250
    elif change == "wall_kind":
        wall.Kind = "curtain"
    elif change == "category":
        door.Category.Id = id_value(-2000011)
    elif change == "placement":
        door.Family.FamilyPlacementType = "face"
    elif change == "missing_width":
        door.width.HasValue = False
    else:
        request["mapping"]["doors"].clear()
    result = read(document, request)
    assert result["status"] == "stopped" and reason in result["message"]
    assert "verified_mapping_json" not in result


def test_unknown_and_unsupported_geometry_is_visible_in_reader_evidence(linked_dwg):
    document, request, primitives, arc, id_value, _ = linked_dwg
    primitives.extend([arc(GraphicsStyleId=id_value(10)), arc(GraphicsStyleId=id_value(-1))])
    request["segment_layers"].append("MISSING")
    result = read(document, request)
    assert result["status"] == "review_required"
    assert {row["layer"] for row in result["unresolved"]} == {"WALL", "ROOM", None, "MISSING"}
    assert len(json.loads(result["snapshot_json"])["segments"]) == 3


def test_refuses_unloaded_link_and_stale_room_point(linked_dwg):
    document, request, *_, reference = linked_dwg
    request["room_points_mm"] = {"stale-room": [0, 0, 3000]}
    assert read(document, request)["status"] == "stopped"
    reference.GetLinkedFileStatus = lambda: "Unloaded"
    assert read(document, request) == {"status": "stopped", "message": "The linked CAD file is not loaded."}


def test_reviewed_room_point_is_bound_to_the_exact_contour(linked_dwg):
    document, request, primitives, *_ = linked_dwg
    candidate, = read(document, request)["room_candidates"]
    request["room_points_mm"] = {candidate["id"]: [-750, 500, 3000]}
    primitives[-1].points[2].Y += 0.01
    result = read(document, request)
    assert result["status"] == "stopped" and "did not match" in result["message"]
