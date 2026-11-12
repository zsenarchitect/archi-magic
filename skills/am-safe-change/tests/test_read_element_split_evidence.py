from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class Id(int):
    Value = property(int)


Id.InvalidElementId = Id(-1)


class Line:
    IsBound = True

    def __init__(self, a, b):
        self.ends = [point(*p) for p in (a, b)]

    def GetEndPoint(self, index):
        return self.ends[index]


class LocationCurve(NS):
    def get_ElementsAtJoin(self, end):
        return self.neighbors[end]


class LocationPoint(NS):
    pass


class Level(NS):
    pass


class Wall(NS):
    pass


class FamilyInstance(NS):
    pass


class Floor(NS):
    pass


class Sketch(NS):
    pass


class PlanarFace(NS):
    pass


class Grid(NS):
    pass


class ModelLine(NS):
    pass


class DetailLine(NS):
    pass


def point(x, y, z):
    return NS(X=x, Y=y, Z=z)


def loop(vertices, z):
    return [Line((*a, z), (*b, z)) for a, b in zip(vertices, vertices[1:] + vertices[:1])]


def native(kind, identifier, **values):
    return kind(Id=Id(identifier), UniqueId=f"uid-{identifier}", **values)


def parameter(identifier, storage, value, **extra):
    return NS(Id=Id(identifier), StorageType=storage, Definition=NS(), HasValue=value is not None, IsReadOnly=False,
              IsShared="GUID" in extra, AsString=lambda: value, AsDouble=lambda: value,
              AsInteger=lambda: value, AsElementId=lambda: value, **extra)


@pytest.fixture
def model(monkeypatch):
    names = ("WALL_BASE_CONSTRAINT", "WALL_BASE_OFFSET", "WALL_HEIGHT_TYPE", "WALL_TOP_OFFSET",
             "WALL_USER_HEIGHT_PARAM", "WALL_TOP_IS_ATTACHED", "WALL_BOTTOM_IS_ATTACHED", "FAMILY_BASE_LEVEL_PARAM",
             "FAMILY_BASE_LEVEL_OFFSET_PARAM", "FAMILY_TOP_LEVEL_PARAM", "FAMILY_TOP_LEVEL_OFFSET_PARAM")
    bip = NS(**{name: name for name in names})
    db = NS(ElementId=Id, Line=Line, Level=Level, Wall=Wall, FamilyInstance=FamilyInstance, Floor=Floor,
            Sketch=Sketch, PlanarFace=PlanarFace, Grid=Grid, ModelLine=ModelLine, DetailLine=DetailLine,
            LocationCurve=LocationCurve, LocationPoint=LocationPoint,
            WallKind=NS(Basic="basic"), WallCrossSection=NS(Vertical="vertical"),
            BuiltInCategory=NS(OST_Columns=-5, OST_StructuralColumns=-6),
            BuiltInParameter=bip, StorageType=NS(String="s", Double="d", Integer="i", ElementId="e"),
            HostObjectUtils=NS(GetTopFaces=lambda floor: ["top"]),
            JoinGeometryUtils=NS(GetJoinedElements=lambda doc, element: []),
            WallUtils=NS(IsWallJoinAllowedAtEnd=lambda wall, end: end == 0))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "pyrevit", NS(api=NS(DB=db)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.syspath_prepend(str(SCRIPTS))

    params = [parameter(5, "s", "", GUID="parameter-guid"), parameter(-7, "e", Id(-8)),
              parameter(8, "s", None), parameter(9, "d", 0.12345678901234567)]
    get_bounds = lambda _: NS(Min=point(0, 0, 0), Max=point(1, 2, 20),
                              Transform=NS(OfPoint=lambda value: value, IsIdentity=True))
    source_fields = dict(ViewSpecific=False, Pinned=False, GroupId=Id(-1), DesignOption=None,
                         Parameters=params, GetTypeId=lambda: Id(100), GetDependentElements=lambda _: [])
    wall_values = {
        "WALL_BASE_CONSTRAINT": parameter(-10, "e", Id(1)), "WALL_BASE_OFFSET": parameter(-11, "d", 0),
        "WALL_HEIGHT_TYPE": parameter(-12, "e", Id(-1)), "WALL_USER_HEIGHT_PARAM": parameter(-13, "d", 20),
        "WALL_TOP_IS_ATTACHED": parameter(-14, "i", 0), "WALL_BOTTOM_IS_ATTACHED": parameter(-15, "i", 0),
    }
    wall = native(Wall, 10, WallType=NS(Kind="basic"), CrossSection="vertical", SketchId=Id(-1),
                  Location=LocationCurve(Curve=Line((0, 0, 0), (4, 0, 0)), neighbors=[[], []]),
                  FindInserts=lambda *_: [], get_Parameter=wall_values.get, get_BoundingBox=get_bounds, **source_fields)
    column_values = {name: parameter(-20 - i, kind, value) for i, (name, kind, value) in enumerate((
        ("FAMILY_BASE_LEVEL_PARAM", "e", Id(1)), ("FAMILY_TOP_LEVEL_PARAM", "e", Id(4)),
        ("FAMILY_BASE_LEVEL_OFFSET_PARAM", "d", 0), ("FAMILY_TOP_LEVEL_OFFSET_PARAM", "d", 0)))}
    columns = [native(FamilyInstance, i, Category=NS(Id=Id(category)), CanSplit=True, IsSlantedColumn=False,
                      Symbol=NS(Family=NS(IsInPlace=False)), SuperComponent=None, Location=LocationPoint(),
                      get_Parameter=column_values.get, get_BoundingBox=get_bounds, **source_fields)
               for i, category in ((11, -5), (12, -6))]
    vertices = [(0, 0), (8, 0), (8, 4), (0, 4)]
    face = PlanarFace(FaceNormal=point(0, 0, 1), GetEdgesAsCurveLoops=lambda: [loop(vertices[::-1], -0.5)])
    floor = native(Floor, 13, SketchId=Id(20), GetSlabShapeEditor=lambda: NS(IsEnabled=False),
                   GetGeometryObjectFromReference=lambda _: face, **source_fields)
    sketch = native(Sketch, 20, OwnerId=Id(13), Profile=[loop(vertices, 0)])
    floor.GetDependentElements = lambda _: [sketch.Id]
    grid = native(Grid, 21, Curve=Line((4, 1, 0), (4, 3, 0)))
    detail_line = native(DetailLine, 22, GeometryCurve=Line((0, 2, 0), (4, 2, 0)))
    elements = [native(Level, i, ProjectElevation=z, Elevation=z + 100) for i, z in enumerate((0, 8, 15, 20), 1)]
    elements.extend([wall, *columns, floor, sketch, grid, detail_line, native(NS, 100)])
    registry = {element.Id.Value: element for element in elements}
    document = NS(IsFamilyDocument=False, IsLinked=False, IsWorkshared=False, IsModified=False, IsModifiable=False,
                  PathName="fixture.rvt", ProjectInformation=NS(UniqueId="fixture-project"),
                  Application=NS(VersionNumber="2026", ShortCurveTolerance=0.0026041666666666665),
                  GetElement=lambda identifier: registry.get(identifier.Value))
    return document, registry, face


def invoke(document, inputs):
    return runpy.run_path(str(SCRIPTS / "read_element_split_evidence.py"),
                          init_globals={"doc": document, "inputs": inputs})["result"]


def request(document):
    host = invoke(document, {"describe": True})["host_identity"]

    def item(i):
        return {"id": str(i), "uid": f"uid-{i}"}

    return {"host_identity": host, "scope": [{"source": item(i), "cuts": [item(2), item(3)]} for i in (10, 11, 12)]
            + [{"source": item(13), "cuts": [item(21), item(22)]}]}


def test_query_produces_bound_typed_source_inventory_and_geometry_preview(model, tmp_path):
    document, _, _ = model
    result = invoke(document, request(document))
    assert result["status"] == "ok", result
    snapshot = result["snapshot_json"].encode()
    inventory = json.loads(result["inventory_json"])
    assert inventory["basis_sha256"] == sha256(snapshot).hexdigest()
    assert inventory["elements"][0]["parameters"] == [
        {"identity": "parameter:-7", "storage": "element_id", "has_value": True, "value": "-8", "read_only": False},
        {"identity": "parameter:8", "storage": "string", "has_value": False, "value": None, "read_only": False},
        {"identity": "parameter:9", "storage": "double", "has_value": True,
         "value": 0.12345678901234567, "read_only": False},
        {"identity": "shared:parameter-guid", "storage": "string", "has_value": True, "value": "", "read_only": False},
    ]
    assert inventory["elements"][-1]["dependencies"] == [{"id": "20", "uid": "uid-20"}]
    assert inventory["elements"][0]["wall_end_joins"] == [
        {"end": 0, "join_allowed": True, "neighbors": []}, {"end": 1, "join_allowed": False, "neighbors": []},
    ]
    assert "fixture.rvt" not in result["snapshot_json"] + result["inventory_json"]
    evidence = tmp_path / "snapshot.json"
    evidence.write_bytes(snapshot)
    run = subprocess.run([sys.executable, str(SCRIPTS / "element_split_preview.py"), str(evidence)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout or run.stderr
    report = json.loads(run.stdout)
    assert report["basis_sha256"] == inventory["basis_sha256"]
    assert [row["category"] for row in report["findings"]] == [
        "wall", "architectural_column", "structural_column", "floor",
    ]
    assert report["findings"][0]["pieces"][1] == {"base_ft": 8, "top_ft": 15}
    assert [piece["area_sqft"] for piece in report["findings"][-1]["pieces"]] == [8, 8, 16]
    assert report["steps"] == []


@pytest.mark.parametrize("change, reason", [
    ("saved_state", "saved, unchanged"),
    ("wrong_cut", "reviewed identity"),
    ("inserts", "contains inserts"),
    ("mismatched_face", "does not match"),
    ("unreadable_parameter", "storage type"),
])
def test_query_blocks_the_whole_snapshot_on_incomplete_source(model, change, reason):
    document, elements, face = model
    args = request(document)
    if change == "saved_state":
        document.IsModified = True
    elif change == "wrong_cut":
        args["scope"][0]["cuts"][0]["uid"] = "other-cut"
    elif change == "inserts":
        elements[10].FindInserts = lambda *_: [Id(77)]
    elif change == "mismatched_face":
        face.GetEdgesAsCurveLoops = lambda: [loop([(0, 0), (9, 0), (9, 4), (0, 4)], -0.5)]
    else:
        elements[10].Parameters = [parameter(1, "unsupported", 1)]
    result = invoke(document, args)
    assert result["status"] == "blocked" and reason in result["reason"]
    assert "snapshot_json" not in result and "inventory_json" not in result
