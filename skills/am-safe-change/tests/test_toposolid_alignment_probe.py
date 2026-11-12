import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_toposolid_alignment.py"


def setup(monkeypatch, fault=None):
    original = [[1, 2, 23], [2, 2, 25], [3, 2, 24]]
    positions = [row[:] for row in original]
    origins = [15, 16, 17]
    operations = []
    document_key = {"uid": "fixture", "path_sha256": "saved"}
    terrain = {"uid": "terrain", "id": "10"}
    support = {"reference": "support:edge", "id": "11", "version": "1"}
    request = {"points_xy_ft": [[1, 2], [2, 2]], "support_reference": "support:edge", "tolerance_ft": 0.01}
    states = NS(Uninitialized="new", Started="started", RolledBack="rolled_back")

    class Transaction:
        state = states.Uninitialized

        def __init__(self, *_):
            operations.append("transaction")

        def Start(self):
            self.state = states.Started
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            operations.append("rollback")
            if fault == "rollback":
                return "pending"
            self.state = states.RolledBack
            if fault != "restore":
                positions[:] = [row[:] for row in original]
            return self.state

    def modify(handle, offset):
        operations.append((handle, offset))
        positions[handle][2] = origins[handle] + (offset * 2 if fault == "offset" else offset)
        if fault == "sentinel":
            positions[2][2] += 1

    def read_snapshot():
        current_support = dict(support)
        if fault == "stale" or fault == "support_drift" and "transaction" in operations and "rollback" not in operations:
            current_support["version"] = "2"
        return {
            "document": document_key, "toposolid": terrain, "vertices_ft": [row[:] for row in positions],
            "alignment": {"blocked": False, "support": current_support, "request": request,
                          "tolerance_ft": request["tolerance_ft"], "rows": [
                              {"point": index + 1, "before_ft": row[:], "after_ft": [row[0], row[1], 31 + index],
                               "support_distance_ft": 0.02 if fault == "separation" else 0.002}
                              for index, row in enumerate(positions[:2])]},
        }

    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        Transaction=Transaction, TransactionStatus=states, ElementId=lambda value: NS(Value=value))))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: None)))
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    for name in ("probe_toposolid_vertex_offset", "read_toposolid_vertices"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        function = runpy.run_path(str(SCRIPT))["probe"]
    finally:
        for name in ("probe_toposolid_vertex_offset", "read_toposolid_vertices"):
            sys.modules.pop(name, None)
    function.__globals__.update(
        project_identity=lambda _: document_key,
        read_vertices=lambda *_: {"snapshot_json": json.dumps(read_snapshot())},
        shape_vertices=lambda *_: (NS(ModifySubElement=modify), [(i, row[:]) for i, row in enumerate(positions)]),
    )
    document = NS(IsReadOnly=False, IsWorkshared=False, IsModelInCloud=False,
                  GetElement=lambda _: terrain, Regenerate=lambda: None,
                  Application=NS(VersionBuild="fixture-build"))
    baseline = read_snapshot()
    baseline["alignment"]["support"] = support
    return function, document, {"baseline": baseline}, operations, positions


def test_alignment_probe_hits_both_vertices_and_rolls_back(monkeypatch):
    probe, document, args, operations, positions = setup(monkeypatch)
    result = probe(document, args)
    assert result["status"] == "measured", result
    assert result["rollback_verified"]
    assert result["vertices"] == [
        {"point": 1, "position_ft": [1, 2, 31], "elevation_error_ft": 0, "support_distance_bound_ft": 0.002},
        {"point": 2, "position_ft": [2, 2, 32], "elevation_error_ft": 0, "support_distance_bound_ft": 0.002},
    ]
    assert operations == ["transaction", (0, 0), (0, 16), (1, 0), (1, 16), "rollback"]
    assert positions == [[1, 2, 23], [2, 2, 25], [3, 2, 24]]


@pytest.mark.parametrize(("fault", "status", "message"), [
    ("offset", "stopped", "did not reach the reviewed target"),
    ("sentinel", "stopped", "calibration moved a vertex"),
    ("support_drift", "stopped", "projected targets changed"),
    ("separation", "stopped", "within the reviewed tolerance"),
    ("rollback", "transaction_unresolved", "verify terrain and support rollback"),
    ("restore", "transaction_unresolved", "verify terrain and support rollback"),
])
def test_alignment_probe_fails_closed(monkeypatch, fault, status, message):
    probe, document, args, operations, _ = setup(monkeypatch, fault)
    result = probe(document, args)
    assert result["status"] == status and message in result["message"]
    assert operations[-1] == "rollback"


def test_stale_support_stops_before_any_transaction(monkeypatch):
    probe, document, args, operations, _ = setup(monkeypatch, "stale")
    with pytest.raises(ValueError, match="changed since preview"):
        probe(document, args)
    assert operations == []
