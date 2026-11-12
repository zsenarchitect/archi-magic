import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_toposolid_vertices.py"


def run_query(monkeypatch, xyz, requested_id="5000000000", enabled=True, elapsed_ms=0):
    class Toposolid:
        Id = NS(Value=5000000000)
        UniqueId = "terrain-uid"

        def GetSlabShapeEditor(self):
            vertices = (NS(Position=NS(X=x, Y=y, Z=z)) for x, y, z in xyz)
            return NS(IsEnabled=enabled, SlabShapeVertices=vertices)

    terrain = Toposolid()
    document = NS(
        Application=NS(VersionNumber="2026"),
        IsFamilyDocument=False,
        PathName="terrain.rvt",
        ProjectInformation=NS(UniqueId="project-uid"),
        GetElement=lambda element_id: terrain if element_id.Value == terrain.Id.Value else None,
    )
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=elapsed_ms))))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        ElementId=lambda value: NS(Value=value), Toposolid=Toposolid)))
    return runpy.run_path(str(SCRIPT), init_globals={
        "doc": document, "inputs": {"toposolid_id": requested_id},
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
