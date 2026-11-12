import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_family_preview.py"


def xyz(x, y, z):
    return NS(X=x, Y=y, Z=z)


class Shift:
    def __init__(self, x=0, y=0, z=0):
        self.offset = (x, y, z)

    def OfPoint(self, point):
        return xyz(*[value + offset for value, offset in zip((point.X, point.Y, point.Z), self.offset)])

    def Multiply(self, other):
        return Shift(*[a + b for a, b in zip(self.offset, other.offset)])


class Solid:
    Volume = 24
    Faces = NS(Size=6)

    def GetBoundingBox(self):
        return NS(Min=xyz(0, 0, 0), Max=xyz(2, 3, 4), Transform=Shift(1, 0, 0), Dispose=lambda: None)


class GeometryInstance:
    Transform = Shift(10, -2, 4)

    def GetSymbolGeometry(self):
        return [Solid()]


class View:
    Id = NS(Value=55)
    UniqueId = "fixture-view"
    Name = "Review 3D"
    ViewType = "ThreeD"
    IsTemplate = IsPerspective = IsLocked = IsSectionBoxActive = CropBoxActive = False
    ViewTemplateId = NS(Value=-1)
    DisplayStyle = "Shading"

    def IsInTemporaryViewMode(self, _mode):
        return False

    def GetTypeId(self):
        return NS(Value=4)

    def GetBackground(self):
        color = NS(Red=238, Green=238, Blue=238)
        return NS(Type="Gradient", SkyColor=color, BackgroundColor=color, GroundColor=color,
                  Dispose=lambda: None)

    def GetOrientation(self):
        return NS(EyePosition=xyz(1, 2, 3), UpDirection=xyz(0, 0, 1), ForwardDirection=xyz(-1, -1, -1))

    GetSavedOrientation = GetOrientation


@pytest.fixture
def host(tmp_path, monkeypatch):
    workspace = tmp_path / "family-previews-fixture"
    workspace.mkdir()
    staged = workspace / "Fixture.rfa"
    staged.write_bytes(b"fixture family")
    request = {"staged_path_sha256": hashlib.sha256(os.path.normcase(str(staged)).encode()).hexdigest(),
               "staged_file_sha256": hashlib.sha256(staged.read_bytes()).hexdigest()}
    view = View()
    family = NS(UniqueId="fixture-family", FamilyCategory=NS(CategoryType="Model"))
    doc = NS(PathName=str(staged), IsFamilyDocument=True, IsLinked=False, OwnerFamily=family,
             ActiveView=view, IsModified=False, IsReadOnly=False,
             FamilyManager=NS(CurrentType=NS(Name="Café")), GetElement=lambda _id: view,
             GetDocumentPreviewSettings=lambda: NS(PreviewViewId=view.Id, IsViewUpdateForced=True,
                                                    Dispose=lambda: None))

    class Collector:
        IsViewValidForElementIteration = staticmethod(lambda document, view_id: view_id.Value == 55)

        def __init__(self, document, view_id=None):
            assert document is doc
            self.view_id = view_id

        def OfClass(self, kind):
            assert self.view_id is None
            if kind == "view":
                return [view]
            assert kind == "view type"
            return [NS(ViewFamily="3D", Id=NS(Value=4), UniqueId="fixture-type")]

        def WhereElementIsNotElementType(self):
            assert self.view_id.Value == 55
            return [NS(Id=NS(Value=9007199254740993), get_Geometry=lambda options: [GeometryInstance()])]

        def Dispose(self):
            pass

    db = NS(View3D=View, View="view", ViewFamilyType="view type", ViewFamily=NS(ThreeDimensional="3D"), XYZ=xyz,
            Options=lambda: NS(Dispose=lambda: None), FilteredElementCollector=Collector,
            Transform=NS(Identity=Shift()), GeometryInstance=GeometryInstance, Solid=Solid,
            CategoryType=NS(Annotation="Annotation"), TemporaryViewMode=NS(
                TemporaryHideIsolate=2, RevealHiddenElements=1, TemporaryViewProperties=4, PreviewFamilyVisibility=8))
    stopwatch = NS(StartNew=lambda: NS(ElapsedMilliseconds=0))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=stopwatch))

    def execute(inputs=request):
        return runpy.run_path(str(SCRIPT), init_globals={"doc": doc, "inputs": inputs})["result"]

    return execute, doc, request, staged, stopwatch


def test_runtime_reports_preview_view_and_transformed_solid_without_changing_document(host):
    execute, document, _, staged, _ = host
    response = execute()
    assert response["status"] == "ok" and response["snapshot_json"].isascii()
    snapshot = json.loads(response["snapshot_json"])
    assert snapshot["classification"] == "solid_geometry_observed"
    assert snapshot["geometry"] == {"element_ids": ["9007199254740993"], "min_ft": [11, -2, 4],
                                    "max_ft": [13, 1, 8]}
    assert snapshot["family_type"] == "Café"
    assert snapshot["active_view"] == snapshot["preview_view"]
    assert snapshot["named_preview_views"] == []
    document.ActiveView.Name = "ARCHI MAGIC FAMILY PREVIEW"
    assert json.loads(execute()["snapshot_json"])["named_preview_views"][0]["uid"] == "fixture-view"
    assert snapshot["preview_view"]["background"]["colors"]["SkyColor"] == [238, 238, 238]
    assert snapshot["preview_view"]["saved_orientation"]["eye_ft"] == [1, 2, 3]
    assert str(staged) not in response["snapshot_json"] and not document.IsModified


def test_runtime_stops_for_wrong_path_changed_bytes_and_incomplete_read(host):
    execute, _, request, staged, clock = host
    assert execute(dict(request, staged_path_sha256="a" * 64))["message"] == (
        "The active family path differs from the verified staged path."
    )
    initial = staged.read_bytes()
    staged.write_bytes(initial + b"changed")
    assert execute()["message"] == "The staged family bytes differ from the verified copy."
    staged.write_bytes(initial)
    clock.StartNew = lambda: NS(ElapsedMilliseconds=10000)
    result = execute()
    assert result == {"status": "stopped", "message": "The family view type read exceeded its limits."}


def test_runtime_leaves_annotation_and_cropped_view_geometry_unclassified(host):
    execute, document, _, _, _ = host
    document.OwnerFamily.FamilyCategory.CategoryType = "Annotation"
    annotation = json.loads(execute()["snapshot_json"])
    assert annotation["classification"] == "annotation" and annotation["geometry"] is None
    document.OwnerFamily.FamilyCategory.CategoryType = "Model"
    document.ActiveView.CropBoxActive = True
    cropped = json.loads(execute()["snapshot_json"])
    assert cropped["classification"] == "uncropped_3d_view_required" and cropped["geometry"] is None


def run_preview(tmp_path, response, intent):
    source, request = tmp_path / "read.json", tmp_path / "intent.json"
    source.write_text(json.dumps({"response": {"status": "ok", "result": response}}))
    request.write_text(json.dumps(intent))
    planner = SCRIPT.parents[2] / "am-safe-change" / "scripts" / "family_preview_plan.py"
    return subprocess.run([sys.executable, str(planner), str(source), str(request)], capture_output=True, text=True)


def test_review_camera_and_deny_reuse_without_saved_family_receipt(host, tmp_path):
    execute, _, _, _, _ = host
    response = execute()
    new = run_preview(tmp_path, response, {"view_type_id": "4"})
    assert new.returncode == 0, new.stderr
    proposed = json.loads(new.stdout)
    assert proposed["steps"] == [{"action": "create", "view_uid": None}]
    assert proposed["ownership_ids"] == []
    camera = proposed["proposed"]["orientation"]
    assert camera["target_ft"] == [12, -0.5, 6]
    assert sum(a * b for a, b in zip(camera["up"], camera["forward"])) == pytest.approx(0)
    assert sum(value * value for value in camera["forward"]) == pytest.approx(1)
    assert proposed["proposed"]["background"]["colors"] == {
        "SkyColor": [238, 238, 238], "BackgroundColor": [238, 238, 238], "GroundColor": [238, 238, 238],
    }
    basis = json.loads(response["snapshot_json"])
    existing = dict(basis["active_view"], name="Archi Magic Family Preview")
    basis["named_preview_views"] = [existing]
    response["snapshot_json"] = json.dumps(basis)
    collision = run_preview(tmp_path, response, {"view_type_id": "4"})
    assert collision.returncode == 2 and "prior receipt" in collision.stderr
    receipt = {"view_uid": existing["uid"], "family_uid": basis["identity"]["family_uid"],
               "saved_file_sha256": "0" * 64}
    wrong_file = run_preview(tmp_path, response, {"view_type_id": "4", "reuse_receipt": receipt})
    assert wrong_file.returncode == 2
    receipt["saved_file_sha256"] = basis["identity"]["staged_file_sha256"]
    reuse = run_preview(tmp_path, response, {"view_type_id": "4", "reuse_receipt": receipt})
    assert reuse.returncode == 0, reuse.stderr
    assert json.loads(reuse.stdout)["steps"] == [{"action": "reuse", "view_uid": existing["uid"]}]
    existing["saved_orientation"] = camera
    basis["preview_view"] = existing
    response["snapshot_json"] = json.dumps(basis)
    same = run_preview(tmp_path, response, {"view_type_id": "4", "reuse_receipt": receipt})
    assert same.returncode == 0, same.stderr
    no_change = json.loads(same.stdout)
    assert no_change["steps"] == [] and no_change["before"] == no_change["after"]
