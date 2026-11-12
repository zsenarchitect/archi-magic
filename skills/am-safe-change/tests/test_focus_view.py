"""Exercise the documented focus-view query, compute, and transaction entry points."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import focus_view_preview as compute


class Id:
    def __init__(self, value):
        self.Value = value


def xyz(x, y, z):
    return NS(X=x, Y=y, Z=z)


WORLD = NS(Origin=xyz(0, 0, 0), BasisX=xyz(1, 0, 0), BasisY=xyz(0, 1, 0),
           BasisZ=xyz(0, 0, 1), OfPoint=lambda point: point)
ROTATED = NS(Origin=xyz(0, 0, 0), BasisX=xyz(0, 1, 0), BasisY=xyz(-1, 0, 0), BasisZ=xyz(0, 0, 1))


def box(low, high, transform=WORLD):
    return NS(Min=xyz(*low), Max=xyz(*high), Transform=transform)


class View:
    IsTemplate = False
    ViewTemplateId = Id(-1)
    Category = NS(Name="Views")

    def __init__(self, identifier, name):
        self.Id = Id(identifier)
        self.UniqueId = "view-" + str(identifier)
        self.Name = name

    def GetPrimaryViewId(self):
        return Id(-1)

    def GetDependentViewIds(self):
        return []

    def GetPlacementOnSheetStatus(self):
        return "NotPlaced"


class View3D(View):
    IsPerspective = False
    IsLocked = False

    def __init__(self, identifier, name):
        super().__init__(identifier, name)
        self.section = box((-5, -5, -5), (5, 5, 5), ROTATED)
        self.IsSectionBoxActive = False

    def GetSectionBox(self):
        return self.section

    def SetSectionBox(self, value):
        self.section = value

    @staticmethod
    def CreateIsometric(doc, family_id):
        assert family_id.Value == 12
        view = View3D(100, "New")
        doc.elements[100] = view
        return view


class ViewPlan(View):
    def __init__(self, identifier, name):
        super().__init__(identifier, name)
        self.CropBox = box((-10, -10, -5), (10, 10, 7), ROTATED)
        self.CropBoxActive = False
        self.split = False

    def get_Parameter(self, _):
        return None

    def GetCropRegionShapeManager(self):
        return NS(ShapeSet=False, Split=self.split)

    def GetViewRange(self):
        return NS(GetLevelId=lambda _: Id(1), GetOffset=lambda plane: float(plane))


class FamilyType:
    DefaultTemplateId = Id(-1)
    Id = Id(12)
    UniqueId = "family-type"
    ViewFamily = "3d"


class ModelElement:
    ViewSpecific = False

    def __init__(self, identifier, low, high):
        self.Id = Id(identifier)
        self.UniqueId = "element-" + str(identifier)
        self.bounds = box(low, high)

    def get_BoundingBox(self, view):
        assert view is None
        return self.bounds


class Document:
    IsFamilyDocument = False
    IsReadOnly = False
    IsWorkshared = False
    PathName = "fixture.rvt"
    ProjectInformation = NS(UniqueId="fixture-project")

    def __init__(self):
        self.elements = {12: FamilyType(), 20: ViewPlan(20, "Plan"), 21: View3D(21, "3D"),
                         30: ModelElement(30, (1, 2, 3), (4, 6, 8)),
                         31: ModelElement(31, (-2, 3, 1), (0, 5, 7))}
        self.ActiveView = self.elements[20]
        self.selection = [Id(31), Id(30)]
        self.transactions = []
        self.bad_regenerate = False

    def GetElement(self, identifier):
        return self.elements.get(identifier.Value)

    def Regenerate(self):
        if self.bad_regenerate:
            self.elements[100].section.Max = xyz(0, 0, 0)


class Transaction:
    def __init__(self, doc, _):
        self.doc = doc
        self.state = "Uninitialized"
        doc.transactions.append(self)

    def Start(self):
        self.state = "Started"
        return self.state

    def GetStatus(self):
        return self.state

    def RollBack(self):
        self.doc.elements.pop(100, None)
        self.state = "RolledBack"
        return self.state

    def Commit(self):
        self.state = "Committed"
        return self.state


@pytest.fixture
def host(monkeypatch):
    doc = Document()
    db = NS(ElementId=Id, View3D=View3D, ViewPlan=ViewPlan, View=View, ViewFamilyType=FamilyType,
            ViewFamily=NS(ThreeDimensional="3d"), RevitLinkInstance=type("Link", (), {}),
            ElementType=type("Type", (), {}), XYZ=xyz, Transform=NS(Identity=WORLD),
            BuiltInParameter=NS(VIEWER_VOLUME_OF_INTEREST_CROP=5),
            PlanViewPlane=NS(TopClipPlane=0, CutPlane=1, BottomClipPlane=2, ViewDepthPlane=3),
            BoundingBoxXYZ=NS, Transaction=Transaction,
            TransactionStatus=NS(Started="Started", Committed="Committed", RolledBack="RolledBack"),
            FilteredElementCollector=lambda model: NS(
                OfClass=lambda cls: [el for el in model.elements.values() if isinstance(el, cls)]))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "pyrevit", NS(api=NS(DB=db)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    ui = NS(Selection=NS(GetElementIds=lambda: doc.selection))

    def run(inputs):
        return runpy.run_path(str(SCRIPTS / "focus_view.py"), init_globals={
            "doc": doc, "inputs": inputs, "__revit__": NS(ActiveUIDocument=ui),
        })["result"]
    return doc, run


def request(action):
    return {"action": action, "target_id": {"create_3d": "12", "reuse_3d": "21", "crop_plan": "20"}[action],
            "name": "Focused review", "padding_ft": 1}


def plan_for(run, action):
    output = run({"request": request(action)})
    assert output["status"] == "ok", output
    return compute.compute(json.loads(output["snapshot_json"]))


@pytest.mark.parametrize("action,low,high", [
    ("create_3d", [-3, 1, 0], [5, 7, 9]),
    ("reuse_3d", [-3, 1, 0], [5, 7, 9]),
    ("crop_plan", [1, -5, -5], [7, 3, 7]),
])
def test_focus_modes_with_independent_readback(host, action, low, high):
    doc, run = host
    plan = plan_for(run, action)
    assert plan["box"]["min"] == low and plan["box"]["max"] == high
    output = run({"mode": "apply", "plan": plan})
    assert output["status"] == "ok", output
    assert doc.transactions[0].state == "Committed"
    verified = run({"mode": "verify", "action": action, "view_id": output["view_id"],
                    "view_uid": output["view_uid"]})
    compute.check_result(plan, verified)
    assert doc.ActiveView.Id.Value == 20
    assert [i.Value for i in doc.selection] == [31, 30]


@pytest.mark.parametrize("change", ["geometry", "identity"])
def test_readback_refuses_changed_elements_with_same_selection_ids(host, change):
    doc, run = host
    plan = plan_for(run, "create_3d")
    output = run({"mode": "apply", "plan": plan})
    assert output["status"] == "ok", output
    if change == "geometry":
        doc.elements[30].bounds.Max = xyz(50, 60, 80)
    else:
        doc.elements[30].UniqueId = "replacement-element"
    verified = run({"mode": "verify", "action": "create_3d", "view_id": output["view_id"],
                    "view_uid": output["view_uid"]})
    assert verified["status"] == "ok", verified
    with pytest.raises(ValueError, match="selected element identity or bounds changed"):
        compute.check_result(plan, verified)


@pytest.mark.parametrize("change", ["geometry", "selection", "name", "model", "plan", "split"])
def test_changed_intent_and_state_stop_before_transaction(host, change):
    doc, run = host
    plan = plan_for(run, "crop_plan" if change == "split" else "create_3d")
    if change == "geometry":
        doc.elements[30].bounds.Max = xyz(5, 6, 8)
    elif change == "selection":
        doc.selection = [Id(30)]
    elif change == "name":
        doc.elements[20].Name = "Focused review"
    elif change == "model":
        doc.PathName = "different.rvt"
    elif change == "split":
        doc.elements[20].split = True
    else:
        plan["box"]["max"] = [999, 999, 999]
    result = run({"mode": "apply", "plan": plan})
    assert result["status"] == "stopped"
    assert doc.transactions == []


def test_mismatched_box_rolls_back(host):
    doc, run = host
    plan = plan_for(run, "create_3d")
    doc.bad_regenerate = True
    result = run({"mode": "apply", "plan": plan})
    assert result["status"] == "stopped"
    assert doc.transactions[0].state == "RolledBack"
    assert 100 not in doc.elements


@pytest.mark.parametrize("action", ["create_3d", "reuse_3d"])
def test_cli_uses_real_telemetry_envelope(host, tmp_path, action):
    _, run = host
    native = run({"request": request(action)})
    read = tmp_path / "read.json"
    read.write_text(json.dumps({"result": native}))
    preview_path = tmp_path / "preview.json"
    cmd = [sys.executable, str(SCRIPTS / "focus_view_preview.py")]
    subprocess.run(cmd + ["compute", str(read), str(preview_path)], check=True)
    plan = json.loads(preview_path.read_text())
    applied = run({"mode": "apply", "plan": plan})
    create = action == "create_3d"
    changes = {"added_count": int(create), "deleted_count": 0, "modified_count": int(not create),
               "by_category": {"Views": 1}}
    if create:
        changes["added_ids"] = [int(applied["view_id"])]
    else:
        changes["modified"] = [{"id": int(applied["view_id"])}]
    dry = tmp_path / "dry.json"
    dry.write_text(json.dumps({"result": applied, "changes": changes}))
    subprocess.run(cmd + ["side-effects", str(preview_path), str(dry)], check=True)
    del changes["by_category"]
    dry.write_text(json.dumps({"result": applied, "changes": changes}))
    assert subprocess.run(cmd + ["side-effects", str(preview_path), str(dry)]).returncode == 2
