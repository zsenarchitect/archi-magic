"""Exercise linked CAD geometry extraction with a simulated Revit geometry tree."""

from hashlib import sha256
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

    element_id.InvalidElementId = element_id(-1)

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

    class ViewPlan(Record):
        pass

    class Phase(Record):
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
    link = Linked(IsLinked=True, UniqueId="aa-1", VersionGuid="link-v1",
                  geometry=[Instance(rotate, [Instance(mirror, primitives)])])
    reference = Record(GetLinkedFileStatus=lambda: "Loaded", GetAbsolutePath=lambda: "drawing.dwg")
    objects = {
        1: link, 2: Level(Id=element_id(2), UniqueId="aa-2", VersionGuid="level-v1", ProjectElevation=feet(3000)),
        3: Record(UniqueId="link-type", VersionGuid="link-type-v1", IsExternalFileReference=lambda: True,
                  GetExternalFileReference=lambda: reference),
        80: WallType(UniqueId="wall-type", VersionGuid="wall-v1", Kind="basic", Width=feet(200.25)),
        81: FamilySymbol(UniqueId="door-type", VersionGuid="door-v1", Category=Record(Id=element_id(-2000014)),
                         Family=Record(FamilyPlacementType="hosted"),
                         width=Record(StorageType="double", HasValue=True, AsDouble=lambda: feet(900))),
        95: Phase(UniqueId="phase-id", VersionGuid="phase-v1"),
        **{id: Style(GraphicsStyleCategory=Record(Name=label)) for id, label in
           [(10, "WALL"), (11, "DOOR"), (12, "ROOM")]},
    }
    db = Record(ElementId=element_id, Transform=Record(Identity=Transform(lambda v: v)),
                GeometryInstance=Instance, Line=Line, PolyLine=Polyline, ImportInstance=Linked,
                Level=Level, GraphicsStyle=Style, Options=Options,
                WallType=WallType, WallKind=Record(Basic="basic"), FamilySymbol=FamilySymbol,
                BuiltInCategory=Record(OST_Doors=-2000014),
                BuiltInParameter=Record(DOOR_WIDTH="door-width", VIEW_PHASE="view-phase"),
                FamilyPlacementType=Record(OneLevelBasedHosted="hosted"),
                StorageType=Record(Double="double", ElementId="element-id"),
                ViewPlan=ViewPlan, ViewType=Record(FloorPlan="floor"), Phase=Phase,
                DesignOption=Record(GetActiveDesignOptionId=lambda doc: doc.active_option),
                ModelPathUtils=Record(ConvertModelPathToUserVisiblePath=lambda value: value))
    api, system = ModuleType("pyrevit.api"), ModuleType("System")
    api.DB, system.Int64 = db, int
    monkeypatch.setitem(sys.modules, "pyrevit.api", api)
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.delitem(sys.modules, "read_linked_cad_layout", raising=False)
    view = ViewPlan(Id=element_id(93), ViewType="floor", IsTemplate=False, UniqueId="plan-id", VersionGuid="plan-v1",
                    GenLevel=objects[2], get_Parameter=lambda _: Record(
                        StorageType="element-id", HasValue=True, AsElementId=lambda: element_id(95)))
    document = Record(IsFamilyDocument=False, IsLinked=False, PathName="/tmp/test-model.rvt",
                      ActiveView=view, active_option=element_id(-1), Application=Record(VersionNumber="2026"),
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


def native_preview(document, request):
    return runpy.run_path(str(SCRIPTS / "preview_cad_layout_in_revit.py"),
                          init_globals={"doc": document, "inputs": request})["result"]


def placement_request(linked_dwg, room_edge_y=200):
    document, source, primitives, *_ = linked_dwg
    for point in (primitives[-1].points[0], primitives[-1].points[1], primitives[-1].points[-1]):
        point.Y = room_edge_y / 304.8
    candidate, = read(document, source)["room_candidates"]
    source["room_points_mm"] = {candidate["id"]: [-750, 500, 3000]}
    source["mapping"] = layout_mapping()
    source["expected_evidence_sha256"] = read(document, source)["evidence_sha256"]
    return {"source": source, "view_id": "93", "phase_id": "95",
            "walls": {"wall-1": {"height_mm": 3000, "room_bounding": True}},
            "doors": {"dwg-1.1.3": {"hand_flipped": True, "facing_flipped": False}}}


def test_native_entry_point_pins_view_and_geometry_after_json_transport(linked_dwg):
    document = linked_dwg[0]
    request = placement_request(linked_dwg)
    first = native_preview(document, request)
    assert first["status"] == "proposed"
    assert native_preview(document, json.loads(json.dumps(request))) == first
    preview = json.loads(first["preview_json"])
    assert preview["model_write_approved"] is False
    assert preview["view"]["phase_uid"] == "phase-id"
    assert preview["wall_choices"]["wall-1"]["height_mm"] == 3000
    assert preview["door_choices"]["dwg-1.1.3"] == {"hand_flipped": True, "facing_flipped": False}
    room, = preview["proposal"]["rooms"]
    assert [item["kind"] for item in room["boundary_edges"]].count("wall_face") == 1
    digest = preview.pop("geometry_sha256")
    canonical = json.dumps(preview, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    assert digest == sha256(canonical.encode("ascii")).hexdigest()
    document.ActiveView.VersionGuid = "plan-v2"
    assert json.loads(native_preview(document, request)["preview_json"])["geometry_sha256"] != digest


def test_native_entry_point_shows_boundary_conflicts_without_a_geometry_digest(linked_dwg):
    request = placement_request(linked_dwg, room_edge_y=0)
    result = native_preview(linked_dwg[0], request)
    assert result["status"] == "review_required"
    preview = json.loads(result["preview_json"])
    assert preview["model_write_approved"] is False
    assert "geometry_sha256" not in preview
    assert any(row["wall_ids"] == ["wall-1"] for row in preview["proposal"]["unresolved"])


@pytest.mark.parametrize("change,reason", [
    ("evidence", "evidence changed"),
    ("phase", "does not match"),
    ("view", "Activate the reviewed floor plan"),
    ("design_option", "Activate the main model"),
    ("wall_choice", "height and room-bounding"),
    ("door_choice", "explicit Boolean"),
])
def test_native_entry_point_refuses_unreviewed_placement(linked_dwg, change, reason):
    document, _, _, _, id_value, _ = linked_dwg
    request = placement_request(linked_dwg)
    if change == "evidence":
        request["source"]["expected_evidence_sha256"] = "0" * 64
    elif change == "phase":
        request["phase_id"] = "96"
    elif change == "view":
        document.ActiveView.ViewType = "ceiling"
    elif change == "design_option":
        document.active_option = id_value(17)
    elif change == "wall_choice":
        request["walls"].clear()
    else:
        request["doors"]["dwg-1.1.3"]["hand_flipped"] = 1
    result = native_preview(document, request)
    assert result["status"] == "stopped" and reason in result["message"]
    assert "preview_json" not in result


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


@pytest.mark.parametrize("change", ["geometry", "type_version", "level", "dwg_path", "document", "missing_layer"])
def test_fresh_layout_read_rejects_drift(linked_dwg, change):
    document, request, primitives, arc, id_value, reference = linked_dwg
    candidate, = read(document, request)["room_candidates"]
    request["room_points_mm"] = {candidate["id"]: [-750, 500, 3000]}
    request["mapping"] = layout_mapping()
    reviewed = read(document, request)
    request["expected_evidence_sha256"] = reviewed["evidence_sha256"]
    assert read(document, request) == reviewed

    if change == "geometry":
        primitives[0].points[1].X += 0.01
    elif change == "type_version":
        document.GetElement(id_value(81)).VersionGuid = "door-v2"
    elif change == "level":
        document.GetElement(id_value(2)).ProjectElevation += 0.01
    elif change == "dwg_path":
        reference.GetAbsolutePath = lambda: "replacement.dwg"
    elif change == "document":
        document.ProjectInformation.UniqueId = "different-project"
    else:
        primitives[2] = arc(GraphicsStyleId=id_value(10))
    result = read(document, request)
    assert result == {"status": "stopped",
                      "message": "The reviewed linked CAD layout evidence changed. Read and review it again."}


def test_linked_host_and_unreviewed_scope_cannot_be_used_for_recheck(linked_dwg):
    document, request, *_ = linked_dwg
    request["expected_evidence_sha256"] = "0" * 64
    assert "reviewed mapping" in read(document, request)["message"]
    document.IsLinked = True
    assert "saved host project" in read(document, request)["message"]


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
