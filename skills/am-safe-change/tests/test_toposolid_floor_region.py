import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_toposolid_floor_region.py"


def element_id(value):
    return NS(Value=value)


def element(cls, value, **fields):
    return cls(Id=element_id(value), UniqueId="uid-{}".format(value),
               VersionGuid="version-{}".format(value), **fields)


def point(x, y, z):
    return NS(X=x, Y=y, Z=z)


class Line:
    IsBound = True

    def __init__(self, start, end):
        self.points = [start, end]

    def GetEndPoint(self, index):
        return point(*self.points[index])


class Arc(Line):
    def Evaluate(self, parameter, normalized):
        assert parameter == 0.5 and normalized
        return point(5, -2, 5)


class Toposolid(NS):
    pass


class Sketch(NS):
    pass


class PlanarFace(NS):
    pass


class Floor(NS):
    pass


class FloorType(NS):
    pass


class Level(NS):
    pass


def top_face(triangles, normal=(0, 0, 1)):
    mesh = NS(NumTriangles=len(triangles),
              get_Triangle=lambda index: NS(get_Vertex=lambda i: point(*triangles[index][i])))
    return PlanarFace(FaceNormal=point(*normal), EdgeLoops=[NS(Size=4)], Triangulate=lambda: mesh)


def closed_loop(corners):
    return [Line(start, end) for start, end in zip(corners, corners[1:] + corners[:1])]


def run_query(monkeypatch, subdivision=False, fault=None, offset=-0.75, candidate_floor=False):
    region_id = 80 if candidate_floor else (11 if subdivision else 10)
    host = element(Toposolid, 10, HostTopoId=element_id(-1))
    outer = [(0, 0, 5), (10, 0, 5), (10, 10, 5), (0, 10, 5)]
    inner = [(4, 4, 5), (4, 6, 5), (6, 6, 5), (6, 4, 5)]
    exterior = closed_loop(outer)
    exterior[0] = Arc(outer[0], outer[1])
    sketch = element(Sketch, 21, OwnerId=element_id(region_id), Profile=[exterior, closed_loop(inner)])
    floor_type = element(FloorType, 31, IsFoundationSlab=fault == "foundation")
    level = element(Level, 41, ProjectElevation=23.0, Elevation=120.0)
    refs = [
        NS(ElementId=element_id(region_id), ConvertToStableRepresentation=lambda _: "face-b", label="b"),
        NS(ElementId=element_id(region_id), ConvertToStableRepresentation=lambda _: "face-a", label="a"),
    ]
    faces = {
        "a": top_face([[(2, 0, 14), (0, 0, 12), (0, 2, 12)]]),
        "b": top_face([[(2, 0, 14), (3, 0, 14), (2, 2, 13)]]),
    }
    region = element(Floor if candidate_floor else Toposolid, region_id,
                     GetTypeId=lambda: floor_type.Id, LevelId=level.Id, HostTopoId=element_id(10 if subdivision else -1),
                     SketchId=element_id(21), refs=refs,
                     GetGeometryObjectFromReference=lambda ref: faces[ref.label],
                     get_Parameter=lambda _: NS(StorageType="double", HasValue=True, AsDouble=lambda: offset))
    if fault == "foreign_face":
        refs[0].ElementId = element_id(10 if subdivision else 999)
    elif fault == "sketch_owner":
        sketch.OwnerId = element_id(999)
    elif fault == "missing_sketch":
        region.SketchId = element_id(-1)
    elif fault == "open_profile":
        exterior.pop()
    elif fault == "degenerate_xy":
        faces["b"] = top_face([[(2, 0, 14), (2, 0, 15), (2, 2, 13)]])
    elif fault == "triangle_cap":
        faces["a"] = top_face([[(2, 0, 14), (0, 0, 12), (0, 2, 12)]] * 3001)
    elif fault == "vertical":
        faces["a"].FaceNormal = point(1, 0, 0)
    elements = {item.Id.Value: item for item in (host, region, sketch, floor_type, level)}
    document = NS(
        Application=NS(VersionNumber="2026"), IsFamilyDocument=False, PathName="terrain.rvt",
        ProjectInformation=NS(UniqueId="fixture"), GetElement=lambda native_id: elements.get(native_id.Value),
    )
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=5000 if fault == "timeout" else 0))))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        ElementId=element_id, Toposolid=Toposolid, Floor=Floor, FloorType=FloorType, Level=Level, Sketch=Sketch,
        Line=Line, Arc=Arc, PlanarFace=PlanarFace, HostObjectUtils=NS(GetTopFaces=lambda item: item.refs),
        BuiltInParameter=NS(TOPOSOLID_SUBDIVIDE_HEIGHT=1, FLOOR_HEIGHTABOVELEVEL_PARAM=2),
        StorageType=NS(Double="double"))))
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.delitem(sys.modules, "read_toposolid_vertices", raising=False)
    inputs = {"floor_id": str(region_id)} if candidate_floor else {
        "region_id": str(region_id), "floor_type_id": "31", "level_id": "41",
    }
    try:
        return runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": inputs})["result"]
    finally:
        sys.modules.pop("read_toposolid_vertices", None)


@pytest.mark.parametrize("subdivision,offset", [(False, None), (True, -0.75), (True, 0.0)])
def test_read_region_preserves_profile_hole_arc_and_selected_grade(monkeypatch, subdivision, offset):
    result = run_query(monkeypatch, subdivision=subdivision, offset=offset)
    assert result["status"] == "ok", result
    snapshot = json.loads(result["snapshot_json"])
    assert snapshot["region"]["id"] == ("11" if subdivision else "10")
    assert snapshot["parent_toposolid"] == (
        {"id": "10", "uid": "uid-10", "version": "version-10"} if subdivision else None)
    assert snapshot["subdivision_offset_ft"] == offset
    assert snapshot["level_project_elevation_ft"] == 23
    assert snapshot["floor_type"]["uid"] == "uid-31"
    assert len(snapshot["profile_loops"]) == 2
    assert snapshot["profile_loops"][0][0] == {"kind": "arc", "points_ft": [[0, 0, 5], [5, -2, 5], [10, 0, 5]]}
    assert snapshot["profile_loops"][1][0]["points_ft"] == [[4, 4, 5], [4, 6, 5]]
    assert snapshot["top_faces"] == [
        {"reference": "face-a", "triangles_ft": [[[0, 0, 12], [0, 2, 12], [2, 0, 14]]]},
        {"reference": "face-b", "triangles_ft": [[[2, 0, 14], [2, 2, 13], [3, 0, 14]]]},
    ]


@pytest.mark.parametrize(("fault", "message"), [
    ("foreign_face", "foreign, missing or repeated"),
    ("sketch_owner", "verifiable sketch of its own"),
    ("missing_sketch", "verifiable sketch of its own"),
    ("open_profile", "empty or open"),
    ("foundation", "non-foundation"),
    ("degenerate_xy", "degenerate XY"),
    ("triangle_cap", "3000 surface triangles"),
    ("vertical", "upward-facing planar"),
    ("timeout", "exceeded 5000 ms"),
])
def test_read_region_rejects_partial_or_unreliable_snapshots(monkeypatch, fault, message):
    result = run_query(monkeypatch, subdivision=True, fault=fault)
    assert set(result) == {"status", "message"}
    assert result["status"] == "stopped"
    assert message in result["message"]



def test_independent_floor_query_reads_actual_floor_dependencies(monkeypatch):
    result = run_query(monkeypatch, candidate_floor=True, offset=1.25)
    assert result["status"] == "ok", result
    snapshot = json.loads(result["snapshot_json"])
    assert snapshot["floor"]["id"] == "80"
    assert snapshot["floor_type"]["uid"] == "uid-31"
    assert snapshot["level"]["uid"] == "uid-41"
    assert snapshot["level_project_elevation_ft"] == 23
    assert snapshot["floor_offset_ft"] == 1.25
    assert len(snapshot["profile_loops"]) == 2
    assert [face["reference"] for face in snapshot["top_faces"]] == ["face-a", "face-b"]
