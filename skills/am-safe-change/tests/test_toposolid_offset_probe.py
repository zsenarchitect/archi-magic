import hashlib
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_toposolid_vertex_offset.py"


def xyz(point):
    return NS(X=point[0], Y=point[1], Z=point[2])


def run_probe(monkeypatch, fault=None):
    baseline_points = [[1, 2, 25.0], [3, 4, 23.0]]
    points = [row[:] for row in baseline_points]
    events = []
    states = NS(Uninitialized="new", Started="started", RolledBack="rolled_back")

    class Transaction:
        state = states.Uninitialized

        def __init__(self, document, title):
            events.append("create")

        def Start(self):
            events.append("start")
            self.state = states.Started
            if fault == "start":
                raise RuntimeError("native start failed after opening")
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            events.append("rollback")
            if fault == "rollback":
                return "pending"
            self.state = states.RolledBack
            if fault != "readback":
                points[:] = [row[:] for row in baseline_points]
            return self.state

    class Terrain:
        Id = NS(Value=10)
        UniqueId = "terrain"
        LevelId = NS(Value=11)

        def GetSlabShapeEditor(self):
            native = [NS(Position=xyz(point), index=index) for index, point in enumerate(points)]

            def modify(vertex, offset):
                assert vertex is native[0]
                events.append(("modify", offset))
                if fault == "native":
                    raise RuntimeError("native modify failed")
                points[vertex.index][2] = 23 + (offset * 2 if fault == "wrong_offset" else offset)
                if fault == "sentinel":
                    points[1][2] += 1

            return NS(IsEnabled=True, SlabShapeVertices=native, ModifySubElement=modify)

    class Level(NS):
        pass

    terrain, level = Terrain(), Level(ProjectElevation=23.0)
    document = NS(
        Application=NS(VersionNumber="2026", VersionBuild="fixture-build"),
        IsFamilyDocument=False, PathName="fixture.rvt", IsReadOnly=False, IsModelInCloud=False,
        IsWorkshared=fault == "workshared", ProjectInformation=NS(UniqueId="project"),
        GetElement=lambda identifier: {10: terrain, 11: level}.get(identifier.Value),
        Regenerate=lambda: events.append("regenerate"),
    )
    baseline = {
        "document": {"uid": "project", "path_sha256": hashlib.sha256(b"fixture.rvt").hexdigest()},
        "toposolid": {"id": "10", "uid": "terrain"}, "vertices_ft": baseline_points,
    }
    if fault == "stale":
        points[1][2] += 1
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        ElementId=lambda value: NS(Value=value), Toposolid=Terrain, Level=Level,
        Transaction=Transaction, TransactionStatus=states)))
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.delitem(sys.modules, "read_toposolid_vertices", raising=False)
    try:
        result = runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": {
            "baseline": baseline, "vertex_ft": baseline_points[0], "offsets_ft": [-1, 3],
        }})["result"]
    finally:
        sys.modules.pop("read_toposolid_vertices", None)
    return result, events, points


def test_probe_measures_offset_origin_and_always_restores_fixture(monkeypatch):
    result, events, points = run_probe(monkeypatch)
    assert result["status"] == "measured", result
    assert result["rollback_verified"]
    assert result["revit_build"] == "fixture-build"
    assert result["level_project_elevation_ft"] == 23
    assert result["measured_offset_origin_ft"] == 23
    assert result["inferred_existing_offset_ft"] == 2
    assert result["samples"] == [
        {"offset_ft": -1, "position_ft": [1, 2, 22]},
        {"offset_ft": 3, "position_ft": [1, 2, 26]},
    ]
    assert points == [[1, 2, 25], [3, 4, 23]]
    assert events == ["create", "start", ("modify", -1), "regenerate", ("modify", 3), "regenerate", "rollback"]


@pytest.mark.parametrize(("fault", "status", "message"), [
    ("start", "stopped", "did not confirm"),
    ("native", "stopped", "could not measure"),
    ("sentinel", "stopped", "another vertex changed"),
    ("wrong_offset", "unqualified", "did not establish"),
    ("rollback", "transaction_unresolved", "did not confirm probe rollback"),
    ("readback", "transaction_unresolved", "baseline could not be verified"),
])
def test_probe_reports_failures_and_attempts_rollback(monkeypatch, fault, status, message):
    result, events, points = run_probe(monkeypatch, fault)
    assert result["status"] == status
    assert message in result["message"]
    assert events[-1] == "rollback"
    if status != "transaction_unresolved":
        assert result["rollback_verified"]
        assert points == [[1, 2, 25], [3, 4, 23]]


@pytest.mark.parametrize(("fault", "message"), [
    ("stale", "changed since the fixture baseline"),
    ("workshared", "local non-workshared fixture"),
])
def test_probe_stops_before_opening_transaction(monkeypatch, fault, message):
    result, events, _ = run_probe(monkeypatch, fault)
    assert result["status"] == "stopped"
    assert message in result["message"]
    assert not events
