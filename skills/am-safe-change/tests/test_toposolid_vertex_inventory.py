import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_toposolid_vertices.py"


def run_query(monkeypatch, xyz, requested_id="5000000000", enabled=True, elapsed_ms=0, alignment=None,
              geometry=None, linked_id=-1, resolved_stable="owner:edge", family=False):
    class Toposolid:
        Id = NS(Value=5000000000)
        UniqueId = "terrain-uid"

        def GetSlabShapeEditor(self):
            vertices = (NS(Position=NS(X=x, Y=y, Z=z)) for x, y, z in xyz)
            return NS(IsEnabled=enabled, SlabShapeVertices=vertices)

    terrain = Toposolid()
    support_id = NS(Value=501)
    class FamilyInstance(NS):
        pass

    owner_class = FamilyInstance if family else NS
    owner = owner_class(Id=support_id, UniqueId="support-uid", GetGeometryObjectFromReference=lambda _: geometry)
    reference = NS(ElementId=support_id, LinkedElementId=NS(Value=linked_id),
                   ConvertToStableRepresentation=lambda _: resolved_stable)
    document = NS(
        Application=NS(VersionNumber="2026"),
        IsFamilyDocument=False,
        PathName="terrain.rvt",
        ProjectInformation=NS(UniqueId="project-uid"),
        GetElement=lambda element_id: {terrain.Id.Value: terrain, support_id.Value: owner}.get(element_id.Value),
    )
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=elapsed_ms))))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        ElementId=lambda value: NS(Value=value), Toposolid=Toposolid,
        Reference=NS(ParseFromStableRepresentation=lambda *_: reference),
        Edge=Edge, Line=Line, PlanarFace=Pad, FamilyInstance=FamilyInstance, XYZ=point)))
    inputs = {"toposolid_id": requested_id}
    if alignment is not None:
        inputs["alignment"] = alignment
    return runpy.run_path(str(SCRIPT), init_globals={
        "doc": document, "inputs": inputs,
    })["result"]


def test_query_reports_existing_vertices_in_model_coordinates(monkeypatch):
    coordinates = [(4, 2, 100), (-3, 9, 102.25), (4, 2, 101)]
    result = run_query(monkeypatch, coordinates)
    assert result["status"] == "ok"
    snapshot = json.loads(result["snapshot_json"])
    assert snapshot["document"]["uid"] == "project-uid"
    assert snapshot["toposolid"] == {"id": "5000000000", "uid": "terrain-uid"}
    assert snapshot["vertices_ft"] == [[-3, 9, 102.25], [4, 2, 100], [4, 2, 101]]
    assert run_query(monkeypatch, list(reversed(coordinates))) == result


@pytest.mark.parametrize(("coordinates", "options", "reason"), [
    ([(0, 1, 2)], {"enabled": False}, "no enabled slab shape editor"),
    ([(0, 1, 2)], {"requested_id": "4"}, "active project"),
    ([(0, 1, 2)], {"requested_id": "9223372036854775808"}, "positive decimal"),
    ([(0, 1, float("nan"))], {}, "non-finite"),
    ([], {}, "no vertices"),
    ([(0, 1, 2)] * 2001, {}, "more than 2000"),
    ([(0, 1, 2)], {"elapsed_ms": 5000}, "after 0 vertices"),
])
def test_query_stops_without_partial_snapshot(monkeypatch, coordinates, options, reason):
    result = run_query(monkeypatch, coordinates, **options)
    assert set(result) == {"status", "message"}
    assert result["status"] == "stopped"
    assert reason in result["message"]


def point(x, y, z):
    return NS(X=x, Y=y, Z=z)


class Line:
    IsBound = True

    def __init__(self, ends):
        self.ends = ends

    def GetEndPoint(self, index):
        return point(*self.ends[index])


class Edge:
    def __init__(self, ends):
        self.ends = ends

    def AsCurve(self):
        return Line(self.ends)


class Pad:
    Origin = point(0, 0, 10)
    FaceNormal = point(-0.5, 0, 1)

    def Project(self, candidate):
        assert candidate.Z == pytest.approx(10 + candidate.X * 0.5)
        if not (0 <= candidate.X <= 10 and 0 <= candidate.Y <= 10):
            return None
        return NS(UVPoint=(candidate.X, candidate.Y), XYZPoint=candidate)

    def IsInside(self, uv):
        x, y = uv
        return not (3 < x < 7 and 3 < y < 7)


def alignment_request(*points, tolerance=0.1):
    return {"points_xy_ft": [list(p) for p in points], "support_reference": "owner:edge",
            "tolerance_ft": tolerance}


def preview(result):
    assert result["status"] == "ok", result
    return json.loads(result["snapshot_json"])["alignment"]


def test_preview_uses_finite_edge_and_keeps_xy(monkeypatch):
    vertices = [(3, 0, 30), (10.05, 0, 29), (11, 0, 28)]
    alignment = alignment_request((3, 0), (10.05, 0), (11, 0))
    edge = Edge([(10, 0, 20), (0, 0, 10)])
    result = preview(run_query(monkeypatch, vertices, alignment=alignment, geometry=edge))
    assert result["blocked"]
    assert result["support"] == {
        "ends_ft": [[0, 0, 10], [10, 0, 20]], "id": "501", "kind": "edge",
        "reference": "owner:edge", "uid": "support-uid",
    }
    assert result["rows"][0]["before_ft"] == [3, 0, 30]
    assert result["rows"][0]["after_ft"] == [3, 0, 13]
    assert result["rows"][1]["after_ft"] == [10.05, 0, 20]
    assert result["rows"][1]["support_distance_ft"] == pytest.approx(0.05)
    assert result["rows"][2]["reason"] == "outside_support"
    assert "after_ft" not in result["rows"][2]


def test_preview_projects_vertically_onto_trimmed_pad(monkeypatch):
    vertices = [(2, 2, 20), (4, 4, 20), (12, 9, 20)]
    alignment = alignment_request((2, 2), (4, 4), (12, 9))
    result = preview(run_query(monkeypatch, vertices, alignment=alignment, geometry=Pad()))
    assert result["blocked"]
    assert result["support"]["kind"] == "pad_face"
    assert result["rows"][0]["after_ft"] == [2, 2, 11]
    assert [row["reason"] for row in result["rows"][1:]] == ["outside_support", "outside_support"]


def test_preview_distinguishes_missing_ambiguous_and_repeated_vertices(monkeypatch):
    vertices = [(2, 0, 20), (2, 0, 22), (3, 0, 30)]
    alignment = alignment_request((2, 0), (9, 0), (3, 0), (3.04, 0))
    edge = Edge([(0, 0, 10), (10, 0, 20)])
    result = preview(run_query(monkeypatch, vertices, alignment=alignment, geometry=edge))
    assert result["blocked"]
    assert [row.get("reason") for row in result["rows"]] == [
        "ambiguous_vertex", "missing_vertex", None, "duplicate_request",
    ]
    assert result["rows"][2]["after_ft"] == [3, 0, 13]


@pytest.mark.parametrize(("edge", "options", "reason"), [
    (Edge([(1, 0, 10), (1, 0, 20)]), {}, "nonvertical XY projection"),
    (Edge([(0, 0, 10), (10, 0, 20)]), {"linked_id": 52}, "active project"),
    (Edge([(0, 0, 10), (10, 0, 20)]), {"resolved_stable": "owner:other"}, "resolved exactly"),
    (Edge([(0, 0, 10), (10, 0, 20)]), {"family": True}, "model-coordinate qualification"),
])
def test_preview_stops_on_unsupported_reference(monkeypatch, edge, options, reason):
    result = run_query(monkeypatch, [(1, 0, 5)], alignment=alignment_request((1, 0)),
                       geometry=edge, **options)
    assert result["status"] == "stopped"
    assert reason in result["message"]


def test_preview_valid_batch_is_unblocked(monkeypatch):
    edge = Edge([(0, 0, 10), (10, 0, 20)])
    result = preview(run_query(monkeypatch, [(3, 0, 5)], alignment=alignment_request((3, 0)), geometry=edge))
    assert not result["blocked"]
    assert result["rows"][0]["after_ft"] == [3, 0, 13]
