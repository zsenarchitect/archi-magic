import json
import math
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "datum_preview.py"


def request():
    return {
        "units": "mm",
        "levels": [{"name": "LEVEL 1", "elevation": 0}, {"name": "Level 2", "spacing": 3000}],
        "grids": [{"axis": "X", "start": -1000, "extent": [12000, -1000],
                   "spacing": "3x8000, 2x6000", "names": ["G1", "G2", "G3", "G4", "G5", "G6"]}],
    }


def test_expansion_existing_names_and_repeat(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import datum_preview as dp

    snapshot = {"project": {}, "request": request(), "existing": [
        {"kind": "level", "name": "Level 1", "id": "1", "uid": "level1", "geometry": 10.0},
        {"kind": "grid", "name": "g1", "id": "2", "uid": "grid1", "geometry": None, "segments": []},
    ]}
    artifact = dp.compute(snapshot)
    plan, shown = artifact["plan"], artifact["preview"]
    assert not shown["blocked"] and shown["steps"] == shown["ownership_ids"] == []
    assert [step["name"] for step in plan["steps"]] == ["Level 2", "G2", "G3", "G4", "G5", "G6"]
    assert plan["steps"][0]["geometry"] == pytest.approx(3000 / 304.8)
    assert plan["steps"][-1]["geometry"][0] == pytest.approx([35000 / 304.8, -1000 / 304.8, 0])
    assert plan["steps"][-1]["geometry"][1] == pytest.approx([35000 / 304.8, 12000 / 304.8, 0])
    assert [row["row"] for row in shown["warnings"]] == ["level:1", "grid:3"]
    assert "differ" in shown["warnings"][0]["text"]
    assert "ID 2" in shown["before"][1][4]

    snapshot["existing"] += [dict(step, id=str(i + 3), uid=f"created-{i}") for i, step in enumerate(plan["steps"])]
    repeat = dp.compute(snapshot)
    assert repeat["plan"]["steps"] == []
    assert len(repeat["preview"]["warnings"]) == 8
    assert not repeat["preview"]["blocked"]


def test_bounds_duplicate_rows_and_endpoint_order(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import datum_preview as dp

    rows = [{"name": str(i), "elevation": 0} for i in range(500)]
    assert len(dp.requested_rows({"units": "ft", "levels": rows})) == 500
    with pytest.raises(ValueError, match="at most 500"):
        dp.requested_rows({"units": "ft", "levels": rows + rows[:1]})
    bad = request()
    bad["grids"][0]["spacing"] = "300x1, 200x1"
    with pytest.raises(ValueError, match="at most 500"):
        dp.requested_rows(bad)
    bad = request()
    bad["grids"][0]["start"] = 99999999
    with pytest.raises(ValueError, match="30,000"):
        dp.requested_rows(bad)
    repeat = {"units": "ft", "levels": [{"name": "A", "elevation": 0}, {"name": "a", "spacing": 2}]}
    artifact = dp.compute({"request": repeat, "existing": [], "project": {}})
    assert artifact["preview"]["blocked"] and artifact["plan"]["steps"] == []
    line = [[0, 1, 0], [0, 2, 0]]
    assert dp.geometry_matches("grid", line, list(reversed(line)))
    assert not dp.geometry_matches("grid", [], line)


def test_native_entry_creates_new_rows_and_rolls_back_late_failure(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import datum_preview as dp

    class Id:
        def __init__(self, value):
            self.Value = int(value)

    class XYZ:
        def __init__(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z

        def DistanceTo(self, other):
            return math.dist((self.X, self.Y, self.Z), (other.X, other.Y, other.Z))

        @staticmethod
        def IsWithinLengthLimits(point):
            return point.DistanceTo(XYZ(0, 0, 0)) < 52000

    class Datum:
        def __init__(self, eid, name):
            self.Id, self.UniqueId, self.Name = Id(eid), f"uid-{eid}", name

        def GetTypeId(self):
            return Id(999)

    class Level(Datum):
        def __init__(self, eid, name, height):
            super().__init__(eid, name)
            self.ProjectElevation = height

        @classmethod
        def Create(cls, doc, height):
            return doc.add(cls(len(doc.items) + 20, "Auto level", height))

    class Line:
        IsBound = True

        def __init__(self, a, b):
            self.points = a, b

        def GetEndPoint(self, index):
            return self.points[index]

        CreateBound = classmethod(lambda cls, a, b: cls(a, b))

    class Grid(Datum):
        def __init__(self, eid, name, curve):
            super().__init__(eid, name)
            self.Curve = curve

        @classmethod
        def Create(cls, doc, curve):
            if doc.fail:
                return None
            return doc.add(cls(len(doc.items) + 20, "Auto grid", curve))

    class MultiSegmentGrid(Datum):
        Text = "G1"

        def GetGridIds(self):
            return [Id(3)]

    class Collector:
        def __init__(self, doc):
            self.doc = doc

        def OfClass(self, cls):
            return (element for element in self.doc.items.values() if isinstance(element, cls))

    events = []
    status = NS(Started="Started", Committed="Committed", RolledBack="RolledBack")

    class Transaction:
        def __init__(self, doc, _title):
            self.doc = doc
            self.previous = dict(doc.items)

        def Start(self):
            events.append("start")
            return status.Started

        def GetStatus(self):
            return status.Started

        def RollBack(self):
            self.doc.items = self.previous
            events.append("rollback")
            return status.RolledBack

        def Commit(self):
            events.append("commit")
            return status.Committed

    db = NS(ElementId=Id, XYZ=XYZ, Level=Level, Grid=Grid, Line=Line, MultiSegmentGrid=MultiSegmentGrid,
            FilteredElementCollector=Collector, Transaction=Transaction, TransactionStatus=status,
            Element=NS(Name=NS(GetValue=lambda element: element.Name)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    doc = NS(IsFamilyDocument=False, IsReadOnly=False, PathName="Synthetic.rvt",
             ProjectInformation=NS(UniqueId="project"), Application=NS(ShortCurveTolerance=0.003),
             Regenerate=lambda: None, fail=True,
             items={1: Level(1, "Level 1", 10), 2: MultiSegmentGrid(2, "unused API name"),
                    3: Grid(3, "child name", Line(XYZ(0, 0, 0), XYZ(1, 0, 0)))})
    doc.GetElement = lambda eid: doc.items.get(eid.Value)
    doc.add = lambda item: doc.items.setdefault(item.Id.Value, item)
    native = SCRIPT.with_name("create_datums.py")

    def run(**inputs):
        return runpy.run_path(str(native), init_globals={"doc": doc, "inputs": inputs})["result"]

    read = run(request=request())
    artifact = dp.compute(dp.snapshot_from(read))
    assert [item["name"] for item in dp.snapshot_from(read)["existing"]] == ["Level 1", "G1"]
    old = dict(doc.items)
    assert run(mode="apply", **artifact)["status"] == "stopped"
    assert events == ["start", "rollback"] and doc.items == old

    doc.items[1].ProjectElevation += 1
    assert run(mode="apply", **artifact)["status"] == "stopped"
    assert events == ["start", "rollback"]
    doc.items[1].ProjectElevation -= 1
    doc.fail = False
    kept = run(mode="apply", **artifact)
    assert kept["status"] == "ok", kept
    result, ids = dp.verify_contents(artifact["plan"], kept)
    assert len(ids) == 6
    additions = [{"id": int(identifier)} for identifier in sorted(ids, key=int)]
    changes = {"added": additions, "added_count": len(ids), "modified": [], "deleted": [],
               "modified_count": 0, "deleted_count": 0, "by_category": {"Datums": len(ids)}}
    dry = {"mode": "dry_run", "status": "ok", "decision": "rolled_back", "run_id": "datum-trial",
           "exit_code": 0, "result_truncated": False, "result": kept, "changes": changes}
    with pytest.raises(ValueError, match="successful rolled-back datum dry run"):
        dp.verify_dry_run(artifact["plan"], dict(dry, mode="modify", decision="committed", approval="user"))
    dp.verify_dry_run(artifact["plan"], {"response": dry})
    for invalid in (dict(changes, added=additions + additions[:1]),
                    dict(changes, added=additions[:-1] + [{"id": 2}]),
                    dict(changes, modified=[{"id": 2}]),
                    dict(changes, by_category={"Datums": len(ids) - 1})):
        with pytest.raises(ValueError, match="telemetry"):
            dp.verify_dry_run(artifact["plan"], dict(dry, changes=invalid))
    verified = run(mode="verify", plan=artifact["plan"], created=result["datums"])
    assert json.loads(verified["datums_json"]) == result
    repeat = dp.compute(dp.snapshot_from(run(request=request())))
    assert run(mode="apply", **repeat)["status"] == "stopped"
    assert events == ["start", "rollback", "start", "commit"]
