from copy import deepcopy
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from plan_dimension_preview import compute, probe_plan


class ElementId:
    def __init__(self, value):
        self.Value = int(value)


class IdList(list):
    Count = property(len)
    Add = list.append

    @classmethod
    def __class_getitem__(cls, _):
        return cls


class Element(NS):
    def IsHidden(self, view):
        return getattr(self, "hidden", False)


class ViewPlan(Element):
    pass


class Grid(Element):
    pass


class Wall(Element):
    pass


class FamilyInstance(Element):
    pass


class Dimension(Element):
    pass


class DimensionType(Element):
    pass


class PlanarFace(NS):
    pass


def point(x, y, z=0):
    return NS(X=x, Y=y, Z=z)


class Line:
    def __init__(self, start, end=None):
        self.IsBound = end is not None
        self.Origin, self.Direction = start, point(-1, 0)
        self.endpoints = [start, end]

    def GetEndPoint(self, index):
        return self.endpoints[index]


def reference(element, name=None):
    return NS(ElementId=element.Id, LinkedElementId=ElementId(-1),
              ConvertToStableRepresentation=lambda document: name or element.UniqueId)


class Collector:
    def __init__(self, document, view_id):
        self.document = document

    def OfClass(self, element_type):
        return (element for element in self.document.elements.values() if isinstance(element, element_type))

    def WherePasses(self, ids):
        return (self.document.GetElement(item) for item in ids if self.document.GetElement(item) is not None)


@pytest.fixture
def runtime(monkeypatch):
    document = NS(
        IsFamilyDocument=False, IsLinked=False, IsReadOnly=False, IsWorkshared=False,
        IsModifiable=False, PathName="synthetic.rvt", elements={},
        ProjectInformation=NS(UniqueId="project"),
        Application=NS(VersionNumber="2026", VersionBuild="fixture", ShortCurveTolerance=0.0026),
    )
    document.GetElement = lambda item: document.elements.get(item.Value)
    document.Equals = lambda other: other is document

    def add(element_type, id_value, **fields):
        element = element_type(Id=ElementId(id_value), UniqueId="element-" + str(id_value), **fields)
        document.elements[id_value] = element
        return element

    sketch = NS(Id=ElementId(50), UniqueId="work-plane",
                GetPlane=lambda: NS(Origin=point(0, 0, 25), Normal=point(0, 0, 1)))
    view = add(ViewPlan, 41, Document=document, ViewType="FloorPlan", IsTemplate=False, CropBoxActive=False,
               GetPrimaryViewId=lambda: ElementId(-1), SketchPlane=sketch,
               RightDirection=point(1, 0), UpDirection=point(0, 1), ViewDirection=point(0, 0, 1))
    dimension_type = add(DimensionType, 51, StyleType="Linear")
    for grid_id, x in ((10, 0), (11, 3), (12, 7)):
        grid = add(Grid, grid_id, IsCurved=False, Station=x)
        grid.GetDatumExtentTypeInView = lambda end, view: "ViewSpecific"
        grid.GetCurvesInView = lambda mode, view, x=x: [Line(point(x, -5), point(x, 5))]

    for wall_id, x, normal in ((20, 1, 1), (21, 8, -1)):
        wall = add(Wall, wall_id, Station=x, Location=NS(Curve=Line(point(x, -5), point(x, 5))))
        face = PlanarFace(FaceNormal=point(normal, 0), Origin=point(x, 0),
                          EdgeLoops=[[NS(AsCurve=lambda x=x: Line(point(x, -5), point(x, 5)))]])
        wall.faces = [reference(wall)]
        wall.GetGeometryObjectFromReference = lambda ref, face=face: face
        wall.FindInserts = lambda *args, wall_id=wall_id: IdList([ElementId(22)] if wall_id == 20 else [])

    opening = add(
        FamilyInstance, 22, Host=document.elements[20], Category=NS(Id=ElementId(-1)),
        Symbol=NS(Id=ElementId(72), UniqueId="window-symbol"), Mirrored=True, HandFlipped=True, FacingFlipped=False,
        GetTransform=lambda: NS(Origin=point(2, 0), BasisX=point(-1, 0), BasisY=point(0, 1), BasisZ=point(0, 0, 1)),
        GetGeometryObjectFromReference=lambda ref: None,
    )
    opening.GetReferences = lambda role: [reference(opening, role)]
    api = ModuleType("pyrevit.api")
    api.DB = NS(
        ElementId=ElementId, ViewPlan=ViewPlan, ViewType=NS(FloorPlan="FloorPlan"),
        Line=Line, Grid=Grid, Wall=Wall, PlanarFace=PlanarFace, FamilyInstance=FamilyInstance,
        Dimension=Dimension, DimensionType=DimensionType, DimensionStyleType=NS(Linear="Linear"),
        FilteredElementCollector=Collector, ElementIdSetFilter=lambda values: values, Reference=reference,
        DatumEnds=NS(End0=0, End1=1), DatumExtentType=NS(Model="Model", ViewSpecific="ViewSpecific"),
        ShellLayerType=NS(Interior="Interior", Exterior="Exterior"),
        HostObjectUtils=NS(GetSideFaces=lambda wall, side: wall.faces),
        BuiltInCategory=NS(OST_Doors=-2, OST_Windows=-1), FamilyInstanceReferenceType=NS(Left="left", Right="right"),
    )
    system = ModuleType("System")
    system.Int64 = int
    generic = ModuleType("System.Collections.Generic")
    generic.List = IdList
    for name, module in (("System", system), ("System.Collections.Generic", generic), ("pyrevit.api", api)):
        monkeypatch.setitem(sys.modules, name, module)

    def run(check_absent_ids=None, **request):
        args = {"request": {"view_id": "41", "dimension_type_id": "51", **request}}
        if check_absent_ids is not None:
            args["check_absent_ids"] = check_absent_ids
        scope = runpy.run_path(str(SCRIPTS / "read_plan_dimensions.py"), init_globals={
            "doc": document, "uidoc": NS(Document=document, ActiveView=view), "inputs": args,
        })
        return scope["result"]

    return run, document, view, dimension_type, add


def row(mode, source_ids, **fields):
    return {"key": mode, "mode": mode, "source_ids": source_ids, "line": [[-1, 0, 25], [9, 0, 25]], **fields}


def read(run, *rows):
    result = run(requests=list(rows))
    assert result["status"] == "ok", result
    return json.loads(result["snapshot_json"])


def test_grid_and_wall_faces_use_the_native_work_plane(runtime):
    run, _, _, _, _ = runtime
    grid = row("grid", ["12", "10", "11"])
    walls = row("overall", ["20", "21"], faces=[{"side": "interior"}, {"side": "exterior"}])
    snapshot = read(run, grid, walls)
    preview, proposals = compute(snapshot)
    assert not preview["blocked"]
    assert snapshot["view"]["sketch_plane"]["uid"] == "work-plane"
    assert snapshot["requests"][0]["witnesses"][0]["trace"] == [[7, -5, 25], [7, 5, 25]]
    assert proposals["grid"]["segments_ft"] == [3, 4]
    assert proposals["overall"]["segments_ft"] == [7]


def test_opening_inventory_and_measured_references_are_bound_to_instance_state(runtime):
    run, document, _, _, _ = runtime
    inventory = run(opening_wall_ids=["20"])
    candidate = inventory["openings"][0]["candidate"]
    opening = row("opening", ["20", "22"])
    initial = read(run, opening)
    assert initial["requests"][0]["opening"] == candidate
    assert "independent trace" in compute(initial)[0]["warnings"][0]["text"]

    opening["opening_evidence"] = {
        "opening": candidate, "traces": [[[6, -1, 25], [6, 1, 25]], [[2, -1, 25], [2, 1, 25]]],
    }
    preview, proposals = compute(read(run, opening))
    assert not preview["blocked"]
    assert proposals["opening"]["segments_ft"] == [4]
    assert [item["stable_ref"] for item in proposals["opening"]["witnesses"]] == ["right", "left"]
    document.elements[22].HandFlipped = False
    assert "stale" in compute(read(run, opening))[0]["warnings"][0]["text"]


def test_unbounded_existing_dimension_is_a_no_op(runtime):
    run, document, _, dimension_type, add = runtime
    add(Dimension, 66, OwnerViewId=ElementId(41), DimensionType=dimension_type,
        AreReferencesAvailable=True, GetTypeId=lambda: dimension_type.Id,
        References=[reference(document.elements[value]) for value in (12, 11, 10)],
        NumberOfSegments=2, Segments=[NS(Value=4), NS(Value=3)], Curve=Line(point(30, 0, 25)))
    preview, proposals = compute(read(run, row("grid", ["10", "11", "12"])))
    assert preview["after"][0][2] == "Already exists"
    assert proposals == {}


def test_mixed_grid_extents_hidden_elements_and_missing_work_plane_are_reported(runtime):
    run, document, view, _, _ = runtime
    grid = row("grid", ["10", "11", "12"])
    document.elements[10].GetDatumExtentTypeInView = lambda end, view: ("Model", "ViewSpecific")[end]
    assert "mixed" in compute(read(run, grid))[0]["warnings"][0]["text"]
    document.elements[11].hidden = True
    warning = compute(read(run, grid))[0]["warnings"][0]["text"]
    assert "10: The grid has mixed" in warning
    assert "11: Source is unavailable" in warning
    view.SketchPlane = None
    assert run(requests=[grid])["status"] == "stopped"


class Changes:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def send(self, document, operation, name, added=(), modified=(), deleted=()):
        event = NS(
            Operation=operation, GetDocument=lambda: document, GetTransactionNames=lambda: [name],
            GetAddedElementIds=lambda: IdList(ElementId(value) for value in added),
            GetModifiedElementIds=lambda: IdList(ElementId(value) for value in modified),
            GetDeletedElementIds=lambda: IdList(ElementId(value) for value in deleted),
        )
        for handler in self.handlers:
            handler(None, event)


@pytest.fixture
def probe(runtime, monkeypatch):
    run, document, view, _, add = runtime
    event = Changes()
    document.Application.DocumentChanged = event
    document.Regenerate = lambda: None
    log = []
    behavior = NS(wrong_value=False, extra_effect=False, late_failure=False, failed_rollback=False)
    status = NS(Uninitialized="Uninitialized", Started="Started", Committed="Committed", RolledBack="RolledBack")

    class Group:
        def __init__(self, doc, name):
            self.doc, self.name = doc, name
            self.state = status.Uninitialized

        def GetStatus(self):
            return self.state

        def Start(self):
            self.baseline = dict(self.doc.elements)
            self.state = status.Started
            return self.state

        def RollBack(self):
            if behavior.failed_rollback:
                raise ValueError("Fixture rollback failed.")
            removed = self.doc.elements.keys() - self.baseline.keys()
            self.doc.elements = self.baseline
            self.state = status.RolledBack
            event.send(self.doc, "TransactionGroupRolledBack", self.name, deleted=removed)
            return self.state

    class Transaction(Group):
        def Commit(self):
            added = self.doc.elements.keys() - self.baseline.keys()
            self.state = status.Committed
            log.append(("commit", sorted(added)))
            event.send(self.doc, "TransactionCommitted", self.name, added=added,
                       modified=[10] if behavior.extra_effect else [])
            return self.state

    def parse(doc, value):
        if value in ("left", "right"):
            return reference(doc.elements[22], value)
        return reference(next(item for item in doc.elements.values() if item.UniqueId == value))

    def new_dimension(owner, line, references, style):
        created = [item for item in document.elements.values() if isinstance(item, Dimension)]
        if behavior.late_failure and created:
            raise ValueError("Fixture second dimension failed.")
        stations = []
        for ref in references:
            source = document.GetElement(ref.ElementId)
            if isinstance(source, FamilyInstance):
                stations.append({"left": 6, "right": 2}[ref.ConvertToStableRepresentation(document)])
            else:
                stations.append(source.Station)
        values = [abs(a - b) for a, b in zip(stations, stations[1:])]
        if behavior.wrong_value:
            values[0] += 0.1
        return add(
            Dimension, 100 + len(created), OwnerViewId=owner.Id,
            DimensionType=style, GetTypeId=lambda: style.Id, AreReferencesAvailable=True,
            References=references, NumberOfSegments=len(values) if len(values) > 1 else 0,
            Segments=[NS(Value=value) for value in values], Value=values[0], Curve=Line(line.Origin),
        )

    class ReferenceArray(list):
        Append = list.append

    api = sys.modules["pyrevit.api"].DB
    monkeypatch.setattr(api.Reference, "ParseFromStableRepresentation", parse, raising=False)
    monkeypatch.setattr(api.Line, "CreateBound", Line, raising=False)
    api.ReferenceArray, api.XYZ = ReferenceArray, point
    api.TransactionGroup, api.Transaction, api.TransactionStatus = Group, Transaction, status
    document.Create = NS(NewDimension=new_dimension)
    exceptions = ModuleType("Autodesk.Revit.Exceptions")
    exceptions.RegenerationFailedException = type("RegenerationFailedException", (Exception,), {})
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.Exceptions", exceptions)

    def trial(plan):
        values = runpy.run_path(str(SCRIPTS / "probe_plan_dimensions.py"), init_globals={
            "doc": document, "uidoc": NS(Document=document, ActiveView=view),
            "inputs": {"plan": plan, "disposable_fixture": True},
        })
        assert not event.handlers
        return values["result"]

    return run, document, trial, behavior, log


def test_probe_measures_grid_wall_and_opening_then_removes_every_temporary_id(probe):
    run, document, trial, _, log = probe
    grids = row("grid", ["10", "11", "12"])
    walls = row("overall", ["20", "21"], faces=[{"side": "interior"}, {"side": "exterior"}])
    candidate = run(opening_wall_ids=["20"])["openings"][0]["candidate"]
    opening = row("opening", ["20", "22"], opening_evidence={
        "opening": candidate, "traces": [[[6, -1, 25], [6, 1, 25]], [[2, -1, 25], [2, 1, 25]]],
    })
    baseline = read(run, grids, walls, opening)
    outcome = trial(probe_plan(baseline))
    assert outcome["status"] == "trial_rolled_back", outcome
    assert outcome["temporary_dimension_ids"] == ["100", "101", "102"]
    assert {name: item["segments_ft"] for name, item in outcome["measurements"].items()} == {
        "grid": [3, 4], "overall": [7], "opening": [4],
    }
    assert log == [("commit", [100, 101, 102])]
    assert read(run, grids, walls, opening) == baseline
    assert all(document.GetElement(ElementId(value)) is None for value in (100, 101, 102))
    fresh = run(requests=[grids, walls, opening], check_absent_ids=outcome["temporary_dimension_ids"])
    assert fresh["confirmed_absent_ids"] == outcome["temporary_dimension_ids"]
    assert json.loads(fresh["snapshot_json"]) == baseline
    assert run(requests=[grids], check_absent_ids=["10"])["status"] == "stopped"


@pytest.mark.parametrize(("behavior_name", "expected_commits", "message"), [
    ("wrong_value", [], "measured dimension"),
    ("extra_effect", [("commit", [100, 101])], "exact Revit change record"),
    ("late_failure", [], "second dimension"),
])
def test_mismatches_rollback_without_leaving_partial_dimensions(probe, behavior_name, expected_commits, message):
    run, _, trial, behavior, log = probe
    requests = [
        row("grid", ["10", "11", "12"]),
        row("overall", ["20", "21"], faces=[{"side": "interior"}, {"side": "exterior"}]),
    ]
    baseline = read(run, *requests)
    setattr(behavior, behavior_name, True)
    outcome = trial(probe_plan(baseline))
    assert outcome["status"] == "stopped", outcome
    assert message in outcome["message"]
    assert outcome["rollback_verified"] is True
    assert log == expected_commits
    assert read(run, *requests) == baseline


def test_stale_source_stops_before_transaction_and_failed_rollback_is_unresolved(probe):
    run, document, trial, behavior, log = probe
    grids = row("grid", ["10", "11", "12"])
    plan = probe_plan(read(run, grids))
    document.elements[10].hidden = True
    assert trial(plan)["status"] == "stopped"
    assert log == []

    document.elements[10].hidden = False
    behavior.failed_rollback = True
    outcome = trial(plan)
    assert outcome["status"] == "transaction_unresolved"
    assert "rollback_verified" not in outcome


def saved_run(run_id, mode, result):
    return {
        "run_id": run_id, "mode": mode, "status": "ok", "decision": "no_changes",
        "exit_code": 0, "result_truncated": False, "error": None, "approval": None,
        "warnings": [], "dialogs": [], "failures": [], "blocked": [],
        "engine": {"requested": "cpython"},
        "changes": {"added_count": 0, "modified_count": 0, "deleted_count": 0, "by_category": {}},
        "result": result,
    }


def test_saved_dimension_probe_requires_independent_absence_and_exact_measurements(probe, tmp_path):
    run, _, trial, _, _ = probe
    rows = [row("grid", ["10", "11", "12"])]
    original = saved_run("a00000000001", "query", run(requests=rows))
    plan = probe_plan(json.loads(original["result"]["snapshot_json"]))
    receipt = trial(plan)
    assert receipt["status"] == "trial_rolled_back"
    dry = saved_run("b00000000002", "dry_run", receipt)
    fresh = saved_run("c00000000003", "query",
                      run(requests=rows, check_absent_ids=receipt["temporary_dimension_ids"]))

    paths = [tmp_path / name for name in ("plan.json", "original.json", "trial.json", "fresh.json")]
    cmd = [sys.executable, str(SCRIPTS / "verify_plan_dimension_probe.py"), *map(str, paths)]

    def verify(*records):
        for path, value in zip(paths, records):
            path.write_text(json.dumps(value))
        return subprocess.run(cmd, capture_output=True, text=True)

    accepted = verify(plan, {"response": original}, dry, fresh)
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout) == {
        "status": "plan_dimension_rollback_verified",
        "basis_sha256": plan["basis_sha256"],
        "revit": {"version": "2026", "build": "fixture"},
        "temporary_dimension_ids": ["100"],
        "original_run_id": "a00000000001",
        "trial_run_id": "b00000000002",
        "fresh_run_id": "c00000000003",
    }

    outer_effects = deepcopy(dry)
    outer_effects["decision"] = "rolled_back"
    outer_effects["changes"].update(
        added_count=1, added=[{"id": 100}], deleted_count=1, deleted=[100],
        transactions=[receipt["transaction"]], by_category={"Dimensions": 1},
    )
    assert verify(plan, original, outer_effects, fresh).returncode == 0

    wrong_effect = deepcopy(outer_effects)
    wrong_effect["changes"]["deleted"] = [101]
    wrong_transaction = deepcopy(outer_effects)
    wrong_transaction["changes"]["transactions"] = ["Different trial"]
    wrong_measurement = deepcopy(dry)
    wrong_measurement["result"]["measurements"]["grid"]["segments_ft"] = [4, 3]
    wrong_view = deepcopy(dry)
    wrong_view["result"]["measurements"]["grid"]["owner_view_id"] = "42"
    wrong_build = deepcopy(dry)
    wrong_build["result"]["revit"]["build"] = "different-build"
    for invalid in (wrong_effect, wrong_transaction, wrong_measurement, wrong_view, wrong_build):
        assert verify(plan, original, invalid, fresh).returncode == 2

    missing_absence = deepcopy(fresh)
    del missing_absence["result"]["confirmed_absent_ids"]
    assert verify(plan, original, dry, missing_absence).returncode == 2
    assert verify(plan, original, dry, dict(fresh, run_id=original["run_id"])).returncode == 2
    assert verify(plan, original, dict(dry, result_truncated=True), fresh).returncode == 2

    drifted = deepcopy(fresh)
    changed = json.loads(drifted["result"]["snapshot_json"])
    changed["view"]["crop_active"] = True
    drifted["result"]["snapshot_json"] = json.dumps(changed)
    assert verify(plan, original, dry, drifted).returncode == 2
