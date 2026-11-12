"""Exercise linked CAD geometry extraction with a simulated Revit geometry tree."""

from hashlib import sha256
from itertools import product
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
            self.IsIdentity = False

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

        def Dispose(self):
            pass

        @staticmethod
        def CreateBound(start, end):
            return Record(start=start, end=end, Dispose=lambda: None)

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

    class Room(Record):
        pass

    class RevitLink(Record):
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

    class NativeList(list):
        Add = list.append

        @classmethod
        def __class_getitem__(cls, _):
            return cls

    class CurveLoop(NativeList):
        Append = list.append

        def Dispose(self):
            pass

    class Solid(Record):
        def __init__(self, low, high):
            sizes = [max(0, b - a) for a, b in zip(low, high)]
            super().__init__(low=low, high=high, Faces=Record(Size=6), Volume=sizes[0] * sizes[1] * sizes[2])

        def Dispose(self):
            pass

    def xyz(x, y, z):
        return Record(X=x, Y=y, Z=z)

    xyz.BasisZ = xyz(0, 0, 1)

    def enclosing_solid(points, height=0):
        coordinates = list(zip(*[(p.X, p.Y, p.Z) for p in points]))
        low = [min(values) for values in coordinates]
        high = [max(values) for values in coordinates]
        high[2] += height
        return Solid(low, high)

    def extrusion(profiles, axis, height):
        assert axis == xyz.BasisZ
        profile, = profiles
        return enclosing_solid([edge.start for edge in profile], height)

    def intersection(a, b, operation):
        assert operation == "intersect"
        return Solid(list(map(max, a.low, b.low)), list(map(min, a.high, b.high)))

    def transform_solid(solid, transform):
        corners = product(*zip(solid.low, solid.high))
        return enclosing_solid([transform.OfPoint(xyz(*corner)) for corner in corners])

    class Collector:
        def __init__(self, document):
            self.document = document

        def OfCategory(self, category):
            self.category = category
            return self

        def WhereElementIsNotElementType(self):
            return iter(self.document.existing.get(self.category, ()))

        def WherePasses(self, _):
            return iter(self.document.tagged)

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
        95: Phase(Id=element_id(95), UniqueId="phase-id", VersionGuid="phase-v1"),
        96: Phase(Id=element_id(96), UniqueId="phase-later", VersionGuid="phase-later-v1"),
        **{id: Style(GraphicsStyleCategory=Record(Name=label)) for id, label in
           [(10, "WALL"), (11, "DOOR"), (12, "ROOM")]},
    }
    categories = {key: key for key in (
        "OST_Walls", "OST_Doors", "OST_Windows", "OST_Rooms", "OST_RoomSeparationLines", "OST_Columns",
        "OST_StructuralColumns", "OST_Floors", "OST_Ceilings", "OST_Roofs", "OST_Curtain_Systems",
        "OST_GenericModel", "OST_RvtLinks")}
    db = Record(ElementId=element_id, Transform=Record(Identity=Transform(lambda v: v)),
                GeometryInstance=Instance, Line=Line, Arc=Arc, PolyLine=Polyline, ImportInstance=Linked,
                XYZ=xyz, CurveLoop=CurveLoop, Solid=Solid, Curve=Line, Point=type("Point", (), {}),
                GeometryCreationUtilities=Record(CreateExtrusionGeometry=extrusion),
                BooleanOperationsUtils=Record(ExecuteBooleanOperation=intersection),
                BooleanOperationsType=Record(Intersect="intersect"), SolidUtils=Record(CreateTransformed=transform_solid),
                ViewDetailLevel=Record(Fine="fine"),
                Level=Level, GraphicsStyle=Style, Options=Options,
                WallType=WallType, WallKind=Record(Basic="basic"), FamilySymbol=FamilySymbol,
                BuiltInCategory=Record(**{**categories, "OST_Doors": -2000014}),
                BuiltInParameter=Record(DOOR_WIDTH="door-width", VIEW_PHASE="view-phase", ROOM_PHASE_ID="room-phase"),
                FamilyPlacementType=Record(OneLevelBasedHosted="hosted"),
                StorageType=Record(Double="double", ElementId="element-id"),
                ViewPlan=ViewPlan, ViewType=Record(FloorPlan="floor"), Phase=Phase,
                DesignOption=Record(GetActiveDesignOptionId=lambda doc: doc.active_option),
                FilteredElementCollector=Collector,
                Architecture=Record(Room=Room), RevitLinkInstance=RevitLink,
                SpatialElementBoundaryOptions=Options, SpatialElementBoundaryLocation=Record(Finish="finish"),
                ModelPathUtils=Record(ConvertModelPathToUserVisiblePath=lambda value: value))
    api, system, generic = ModuleType("pyrevit.api"), ModuleType("System"), ModuleType("System.Collections.Generic")
    storage = ModuleType("Autodesk.Revit.DB.ExtensibleStorage")
    api.DB, system.Int64, system.Guid, system.String, generic.List = db, int, str, str, NativeList
    storage.Schema = Record(Lookup=lambda _: document.schema)
    storage.ExtensibleStorageFilter = lambda _: Record(Dispose=lambda: None)
    storage.ContainerType = Record(Simple="simple")
    monkeypatch.setitem(sys.modules, "pyrevit.api", api)
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", generic)
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.DB.ExtensibleStorage", storage)
    monkeypatch.delitem(sys.modules, "read_linked_cad_layout", raising=False)
    monkeypatch.delitem(sys.modules, "read_cad_layout_surroundings", raising=False)
    monkeypatch.delitem(sys.modules, "check_cad_wall_collisions", raising=False)
    monkeypatch.delitem(sys.modules, "read_cad_room_boundaries", raising=False)
    monkeypatch.delitem(sys.modules, "cad_layout_provenance", raising=False)
    view = ViewPlan(Id=element_id(93), ViewType="floor", IsTemplate=False, UniqueId="plan-id", VersionGuid="plan-v1",
                    GenLevel=objects[2], get_Parameter=lambda _: Record(
                        StorageType="element-id", HasValue=True, AsElementId=lambda: element_id(95)))
    document = Record(IsFamilyDocument=False, IsLinked=False, PathName="/tmp/test-model.rvt", existing={}, db=db,
                      schema=None, tagged=[],
                      ActiveView=view, active_option=element_id(-1), Application=Record(VersionNumber="2026"),
                      ProjectInformation=Record(UniqueId="aa-3"))
    document.GetElement = lambda id: objects.get(id.Value) or next(
        (item for rows in document.existing.values() for item in rows if item.Id.Value == id.Value), None)
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
    assert preview.pop("existing_model_scan")["status"] == "none_found"
    assert preview.pop("wall_collision_scan")["status"] == "none_measured"
    assert preview.pop("existing_room_boundaries")["status"] == "none_found"
    assert preview.pop("provenance")["status"] == "no_tags"
    manifest = preview.pop("placement_manifest")
    assert sorted(piece["kind"] for piece in manifest["pieces"].values()) == [
        "door", "room", "separator", "separator", "separator", "wall"]
    digest = preview.pop("geometry_sha256")
    canonical = json.dumps(preview, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    assert digest == sha256(canonical.encode("ascii")).hexdigest()
    document.ActiveView.VersionGuid = "plan-v2"
    assert json.loads(native_preview(document, request)["preview_json"])["geometry_sha256"] != digest


def existing_element(id_value, identifier, bounds, *, placed=True, created=96, demolished=-1):
    def xyz(xyz_mm):
        return Record(**{key: value / 304.8 for key, value in zip("XYZ", xyz_mm)})

    def get_bounding_box(view):
        assert view is None
        if bounds is None:
            return None
        return Record(Min=xyz(bounds[0]), Max=xyz(bounds[1]), Enabled=True, Dispose=lambda: None)

    return Record(Id=id_value(identifier), UniqueId=f"existing-{identifier}", VersionGuid="existing-v1",
                  CreatedPhaseId=id_value(created), DemolishedPhaseId=id_value(demolished),
                  Location=object() if placed else None, get_BoundingBox=get_bounding_box)


def tagged_piece(id_value, identifier, manifest, key, *, copied=False):
    uid = f"tagged-{identifier}"
    piece = manifest["pieces"][key]
    payload = json.dumps({"scope": manifest["scope"], "lineage_sha256": manifest["lineage_sha256"],
                          "plan_sha256": manifest["plan_sha256"], "piece_key": key, "kind": piece["kind"],
                          "element_uid": "different-element" if copied else uid,
                          "expected_sha256": piece["expected_sha256"], "observed_sha256": "a" * 64})

    class EntityGet:
        def __getitem__(self, _):
            return lambda _field: payload

    entity = Record(IsValid=lambda: True, Get=EntityGet())
    return Record(Id=id_value(identifier), UniqueId=uid, GetEntity=lambda _: entity)


def test_preview_detects_partial_copied_and_moved_cad_placements(linked_dwg):
    document, _, _, _, id_value, _ = linked_dwg
    request = placement_request(linked_dwg)

    def preview():
        return json.loads(native_preview(document, request)["preview_json"])

    manifest = preview()["placement_manifest"]
    document.schema = Record(SchemaName="ArchiMagicCadLayoutV1", ListFields=lambda: [
        Record(FieldName="Payload", ValueType=Record(FullName="System.String"), ContainerType="simple")])
    document.tagged = [tagged_piece(id_value, index, manifest, key)
                       for index, key in enumerate(manifest["pieces"], 100)]
    assert preview()["provenance"]["status"] == "matching_tags_pending_readback"
    removed = document.tagged.pop()
    partial = preview()["provenance"]
    assert partial["status"] == "conflict" and len(partial["missing_piece_keys"]) == 1
    document.tagged.append(tagged_piece(id_value, 105, manifest, partial["missing_piece_keys"][0], copied=True))
    assert preview()["provenance"]["conflicts"] == [{"id": "105", "reason": "copied_provenance"}]
    document.tagged[-1] = removed

    link = document.GetElement(id_value(1))
    original = link.geometry[0].Transform.OfPoint

    def moved(point):
        value = original(point)
        return Record(X=value.X + 10000 / 304.8, Y=value.Y, Z=value.Z)

    link.geometry[0].Transform.OfPoint = moved
    source = request["source"]
    del source["expected_evidence_sha256"]
    source["room_points_mm"] = {}
    candidate, = read(document, source)["room_candidates"]
    source["room_points_mm"] = {candidate["id"]: [9250, 500, 3000]}
    source["expected_evidence_sha256"] = read(document, source)["evidence_sha256"]
    changed = preview()
    assert changed["placement_manifest"]["lineage_sha256"] == manifest["lineage_sha256"]
    assert changed["placement_manifest"]["plan_sha256"] != manifest["plan_sha256"]
    assert changed["existing_model_scan"]["status"] == "none_found"
    assert changed["provenance"]["status"] == "conflict"
    assert {row["reason"] for row in changed["provenance"]["conflicts"]} == {"stale_placement"}


def test_native_scan_finds_model_candidates_including_later_phases_and_reports_unknowns(linked_dwg):
    document, _, _, _, id_value, _ = linked_dwg
    request = placement_request(linked_dwg)
    document.existing = {
        "OST_Rooms": [
            existing_element(id_value, 103, ((0, -100, 3100), (600, 100, 3400))),
            existing_element(id_value, 104, None, placed=False),
        ],
        "OST_RvtLinks": [existing_element(id_value, 101, ((490, -100, 3000), (720, 100, 3100)))],
        "OST_Walls": [
            existing_element(id_value, 105, ((540, -500, 7200), (720, 100, 7300))),
            existing_element(id_value, 107, None),
        ],
    }
    result = native_preview(document, request)
    assert result["status"] == "proposed"
    scan = json.loads(result["preview_json"])["existing_model_scan"]
    assert scan["status"] == "incomplete" and scan["unplaced_rooms"] == 1
    assert scan["unavailable"] == [{"id": "107", "category": "OST_Walls", "reason": "no_model_bounds"}]
    assert [row["id"] for row in scan["candidates"]] == ["101", "103"]
    link, room = scan["candidates"]
    assert link["category"] == "OST_RvtLinks" and "wall-1" in link["near"]
    assert room["created_phase_id"] == "96" and "wall-1" in room["near"]


def test_native_scan_reports_truncation_instead_of_empty_success(linked_dwg, monkeypatch):
    document, _, _, _, id_value, _ = linked_dwg
    request = placement_request(linked_dwg)
    native_preview(document, request)
    scan_module = sys.modules["read_cad_layout_surroundings"]
    monkeypatch.setattr(scan_module, "MAX_VISITED", 1)
    document.existing = {"OST_Walls": [
        existing_element(id_value, identifier, ((0, 0, 9000), (10, 10, 9010))) for identifier in (101, 102)
    ]}
    scan = json.loads(native_preview(document, request)["preview_json"])["existing_model_scan"]
    assert scan["status"] == "incomplete" and scan["limit"] == "elements" and scan["visited"] == 1
    assert scan["candidates"] == []


def test_native_wall_measurement_distinguishes_contact_overlap_and_unavailable_geometry(linked_dwg):
    document, _, _, _, id_value, _ = linked_dwg
    request = placement_request(linked_dwg)
    overlap = ((610, 0, 3000), (690, 100, 3100))
    contact = ((610, 0, 2900), (690, 100, 3000))
    elements = [existing_element(id_value, number, box)
                for number, box in ((101, overlap), (102, contact), (103, overlap))]
    to_feet = lambda point: [coordinate / 304.8 for coordinate in point]
    transform = type(document.db.Transform.Identity)
    shift_x = transform(lambda point: Record(X=point.X + 600 / 304.8, Y=point.Y, Z=point.Z))
    shift_z = transform(lambda point: Record(X=point.X, Y=point.Y, Z=point.Z + 3000 / 304.8))
    local = ((10, 0, 0), (90, 100, 100))
    elements[0].get_Geometry = lambda _: [document.db.GeometryInstance(shift_x, [
        document.db.GeometryInstance(shift_z, [document.db.Solid(*map(to_feet, local))])
    ])]
    elements[1].get_Geometry = lambda _: [document.db.Solid(*map(to_feet, contact))]
    elements[2].get_Geometry = lambda _: [object()]
    document.existing = {"OST_Walls": elements}
    scan = json.loads(native_preview(document, request)["preview_json"])["wall_collision_scan"]
    assert scan["status"] == "incomplete" and scan["overlap_count"] == 1
    assert [row["id"] for row in scan["measured"]] == ["101", "102"]
    assert scan["measured"][0]["maximum_overlap_mm3"] == pytest.approx(800000)
    assert scan["measured"][1]["maximum_overlap_mm3"] == 0
    assert scan["unresolved"] == [{"id": "103", "walls": ["wall-1"], "reason": "unsupported_geometry"}]


def test_room_boundary_digest_tracks_later_phase_geometry_and_linked_sources(linked_dwg):
    document, _, _, _, id_value, _ = linked_dwg
    request = placement_request(linked_dwg)
    box = ((500, -100, 3100), (900, 500, 5500))
    host_wall = existing_element(id_value, 101, box)
    linked_wall = Record(UniqueId="linked-wall-v1")
    link = document.db.RevitLinkInstance(**vars(existing_element(id_value, 102, box)),
                                        GetLinkDocument=lambda: Record(GetElement=lambda _: linked_wall))
    room = document.db.Architecture.Room(**vars(existing_element(id_value, 103, box, created=95)))
    room.LevelId = id_value(2)
    room.Location = Record(Point=document.db.XYZ(700 / 304.8, 200 / 304.8, 3200 / 304.8))
    room.DesignOption = None
    room.Area = 240000 / 304.8 ** 2
    room.get_Parameter = lambda name: Record(StorageType="element-id", HasValue=True,
                                            AsElementId=lambda: id_value(96)) if name == "room-phase" else None
    document.existing = {"OST_Walls": [host_wall], "OST_RvtLinks": [link], "OST_Rooms": [room]}

    def segment(start, end, source=101, linked=-1):
        return Record(ElementId=id_value(source), LinkElementId=id_value(linked),
                      GetCurve=lambda: document.db.Line(10, start, end), Dispose=lambda: None)

    def loops(options):
        assert options.SpatialElementBoundaryLocation == "finish"
        return [ring]

    def observations():
        return json.loads(native_preview(document, request)["preview_json"])["existing_room_boundaries"]

    a, b, c, d = (500, -100, 3100), (900, -100, 3100), (900, 500, 3100), (500, 500, 3100)
    ring = [segment(a, b, 102, 88), segment(b, c), segment(c, d), segment(d, a)]
    room.GetBoundarySegments = loops
    first = observations()
    assert first["status"] == "measured", first
    row, = first["rooms"]
    assert row["phase_id"] == "96" and row["phase_uid"] == "phase-later"
    assert row["segment_count"] == 4 and row["native_area_mm2"] == pytest.approx(240000)
    assert row["boundary_sources"] == [["101", "existing-101", "", ""],
                                       ["102", "existing-102", "88", "linked-wall-v1"]]
    ring = [segment(c, b), segment(b, a, 102, 88), segment(a, d), segment(d, c)]
    assert observations() == first

    linked_wall.UniqueId = "linked-wall-reloaded"
    assert observations()["rooms"][0]["boundary_sha256"] != row["boundary_sha256"]
    c = (900, 520, 3100)
    ring = [segment(a, b, 102, 88), segment(b, c), segment(c, d), segment(d, a)]
    moved = observations()
    assert moved["rooms"][0]["boundary_sha256"] != row["boundary_sha256"]
    ring.pop()
    stopped = observations()
    assert stopped["status"] == "incomplete" and stopped["rooms"] == []
    assert stopped["unresolved"] == [{"id": "103", "reason": "open_or_repeated_room_boundary"}]


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
