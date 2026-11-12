import json
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

    def __init__(self, boundary):
        self.boundary = boundary

    def GetBoundarySegments(self, options):
        assert options.SpatialElementBoundaryLocation == "finish"
        return self.boundary


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
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=ListFactory()))

    def execute(rooms, ids):
        # The document stand-in has no transaction or write APIs.
        doc = NS(IsFamilyDocument=False, GetElement=rooms.get)
        result = runpy.run_path(str(SCRIPT), init_globals={"doc": doc, "inputs": {"room_ids": ids}})
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


class Phase(Element):
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
        4: template, 8: Phase(8), 9: Phase(9), room.Id.Value: room,
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
        View=View, ViewPlan=ViewPlan, Phase=Phase, DesignOption=design_option, ViewFamilyType=view_family_type,
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
