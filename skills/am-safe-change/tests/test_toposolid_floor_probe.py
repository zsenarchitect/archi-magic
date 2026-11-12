import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_toposolid_floor_conversion.py"


def loop(corners):
    return [{"kind": "line", "points_ft": [a, b]} for a, b in zip(corners, corners[1:] + corners[:1])]


def run_probe(monkeypatch, fault=None, shaped=False):
    calls = []
    states = NS(Uninitialized="new", Started="started", RolledBack="rolled_back")
    identifier = NS(Value=81)
    elements = {}
    offset = [0]
    vertices = [[0, 0, 10], [2, 0, 10], [2, 2, 10], [0, 2, 10]]

    class Transaction:
        state = states.Uninitialized

        def __init__(self, document, name):
            calls.append("transaction")

        def Start(self):
            self.state = states.Started
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            calls.append("rollback")
            if fault == "rollback":
                return "pending"
            self.state = states.RolledBack
            if fault != "retained":
                elements.clear()
            return self.state

    class NativeList(list):
        @classmethod
        def __class_getitem__(cls, _):
            return cls

        def Add(self, item):
            self.append(item)

    class CurveLoop(list):
        Append = list.append

    class Floor:
        @staticmethod
        def Create(document, profiles, floor_type, level):
            assert len(profiles) == 2
            assert profiles[0][0] == ("arc", (0, 0, 9), (2, 0, 9), (1, -1, 9))
            assert profiles[1][0] == ("line", (0.8, 0.8, 9), (0.8, 1.2, 9))
            calls.append(("create", floor_type.Value, level.Value))
            floor = Floor()
            elements[identifier.Value] = floor
            return floor

        Id = identifier

        def get_Parameter(self, _):
            return NS(IsReadOnly=False, StorageType="double", Set=set_offset, AsDouble=lambda: offset[0])

        def GetSlabShapeEditor(self):
            return editor

    def set_offset(value):
        offset[0] = value
        calls.append(("offset", value))
        return True

    def modify(vertex, offset_ft):
        calls.append(("boundary", vertex, offset_ft))
        vertices[vertex][2] = 10 + offset_ft

    def add_points(points):
        calls.append(("interior", list(points)))
        vertices.extend(list(point) for point in points)

    def split(first, second):
        calls.append(("split", first, second))

    editor = NS(IsEnabled=True, ModifySubElement=modify, AddPoints=add_points, AddSplitLine=split)
    db = NS(
        XYZ=lambda *args: tuple(args), CurveLoop=CurveLoop, Floor=Floor,
        Line=NS(CreateBound=lambda a, b: ("line", a, b)), Arc=NS(Create=lambda a, b, c: ("arc", a, b, c)),
        BoundaryValidation=NS(IsValidHorizontalBoundary=lambda profiles: fault != "boundary"),
        Transaction=Transaction, TransactionStatus=states, StorageType=NS(Double="double"),
        BuiltInParameter=NS(FLOOR_HEIGHTABOVELEVEL_PARAM=1), ElementId=lambda value: NS(Value=value),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: None)))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=NativeList))
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    for name in ("probe_toposolid_vertex_offset", "read_toposolid_floor_region", "read_toposolid_vertices"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        function = runpy.run_path(str(SCRIPT))["probe"]
    finally:
        for name in ("probe_toposolid_vertex_offset", "read_toposolid_floor_region", "read_toposolid_vertices"):
            sys.modules.pop(name, None)

    outer = loop([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]])
    outer[0] = {"kind": "arc", "points_ft": [[0, 0, 0], [1, -1, 0], [2, 0, 0]], "center_ft": [1, 0, 0]}
    inner = loop([[0.8, 0.8, 0], [0.8, 1.2, 0], [1.2, 1.2, 0], [1.2, 0.8, 0]])
    common = {
        "document": {"uid": "fixture", "path_sha256": "saved"},
        "floor_type": {"id": "31", "uid": "type"},
        "level": {"id": "41", "uid": "level"},
        "level_project_elevation_ft": 9,
        "profile_loops": [outer, inner],
        "top_faces": [{"triangles_ft": [[[0, 0, 11], [2, 0, 10], [0.5, 0.5, 10]]]}],
    }
    baseline = dict(common, region={"id": "10", "uid": "terrain"})

    def read_region(document, request):
        assert request == {"region_id": "10", "floor_type_id": "31", "level_id": "41"}
        result = dict(baseline)
        if fault == "stale" or fault == "source_changed" and "rollback" in calls:
            result["region"] = {"id": "10", "uid": "changed"}
        return {"snapshot_json": json.dumps(result)}

    def read_floor(document, args):
        assert args == {"floor_id": "81"} and identifier.Value in elements
        if fault == "native_read":
            raise RuntimeError("read failed")
        result = dict(common, floor={"id": "81", "uid": "floor"}, floor_offset_ft=offset[0])
        if fault == "grade":
            result["top_faces"] = [{"triangles_ft": [[[0, 0, 11], [2, 0, 10], [0.5, 0.5, 11]]]}]
        return {"snapshot_json": json.dumps(result)}

    function.__globals__.update(project_identity=lambda _: common["document"], read_region=read_region,
                                read_floor=read_floor, shape_vertices=lambda *_: (editor, list(enumerate(vertices))))
    document = NS(
        Application=NS(VersionBuild="fixture-build"), IsReadOnly=False, IsWorkshared=False,
        IsModelInCloud=False, Regenerate=lambda: calls.append("regenerate"),
        GetElement=lambda native_id: elements.get(native_id.Value),
    )
    shape = {"boundary_ft": [], "interior_ft": [], "split_lines_xy_ft": []}
    if shaped:
        shape = {"boundary_ft": [[0, 0, 11]], "interior_ft": [[0.5, 0.5, 10]],
                 "split_lines_xy_ft": [[[0, 0], [0.5, 0.5]]]}
    return function, document, {
        "baseline": baseline, "floor_offset_ft": 1.0, "xy_tolerance_ft": 0,
        "area_tolerance_sqft": 0, "elevation_tolerance_ft": 0.1, "shape": shape,
    }, calls


def test_floor_probe_rebuilds_profile_and_grade_operations_and_rolls_back(monkeypatch):
    probe, document, request, calls = run_probe(monkeypatch, shaped=True)
    result = probe(document, request)
    assert result["status"] == "measured", result
    assert result["rollback_verified"] and result["geometry"]["matches_floor"]
    assert ("create", 31, 41) in calls
    assert ("offset", 1) in calls
    assert ("boundary", 0, 1) in calls
    assert ("interior", [(0.5, 0.5, 10)]) in calls
    assert ("split", 0, 4) in calls
    assert calls[-1] == "rollback"


@pytest.mark.parametrize(("fault", "status"), [
    ("grade", "mismatch"), ("native_read", "stopped"), ("rollback", "transaction_unresolved"),
    ("retained", "transaction_unresolved"), ("source_changed", "transaction_unresolved"),
])
def test_floor_probe_reports_grade_mismatch_or_failed_rollback(monkeypatch, fault, status):
    probe, document, request, calls = run_probe(monkeypatch, fault=fault)
    assert probe(document, request)["status"] == status
    assert calls[-1] == "rollback"


@pytest.mark.parametrize(("fault", "message"), [("stale", "changed"), ("boundary", "refused")])
def test_floor_probe_stops_before_transaction_for_stale_input_or_invalid_profile(monkeypatch, fault, message):
    probe, document, request, calls = run_probe(monkeypatch, fault=fault)
    with pytest.raises(ValueError, match=message):
        probe(document, request)
    assert not calls
