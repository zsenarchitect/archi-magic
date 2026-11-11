import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compound_legend_preview.py"
READER = SCRIPT.with_name("read_compound_legend.py")


def layer(width, material="Material A", variable=False, function="Structure"):
    return {"width_ft": width, "material": material, "material_id": "99",
            "variable": variable, "function": function}


def test_cli_widths_separate_nominal_and_vertical_slice(tmp_path):
    record, plan, html, preview = (tmp_path / name for name in ("read.json", "plan.json", "legend.html", "preview.json"))
    vertical = {"id": "9007199254740993", "name": "Wall <script>alert(1)</script>", "category": "Wall",
                "instances": 2, "width_ft": 0.5, "grid_width_ft": 0.5,
                "layers": [layer(0.3), layer(0.1, variable=True)],
                "slice": {"width_ft": 0.25, "height_ft": 4.0,
                          "layers": [layer(0.0, function="Membrane"), layer(0.25, "By Category")]}}
    floor = {"id": "16", "name": "Floor A", "category": "Floor", "instances": 1,
             "width_ft": 1.0, "layers": [layer(0.25), layer(0.75)]}
    snapshot = {"request": {}, "types": [vertical, floor]}

    def run():
        record.write_text(json.dumps({"result": {"status": "ok", "snapshot_json": json.dumps(snapshot)}}))
        return subprocess.run([sys.executable, SCRIPT, record, plan, "--html", html, "--preview", preview],
                              capture_output=True, text=True)

    result = run()
    assert result.returncode == 0, result.stderr
    data = json.loads(plan.read_text())
    items = data["plan"]["types"]
    assert [item["id"] for item in items] == ["16", "9007199254740993"]
    assert items[1]["section_reference_ft"] == 0.25
    assert items[1]["nominal_sum_ft"] == 0.4
    assert data["preview"] == json.loads(preview.read_text())
    assert not data["preview"]["blocked"]
    page = html.read_text()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "Zero-thickness layer 1" in page
    assert "Nominal layers" in page and "152.400 mm" in page

    floor["width_ft"] = 1.01
    vertical["width_ft"] = None
    result = run()
    assert result.returncode == 1
    change = json.loads(preview.read_text())
    assert change["blocked"] and change["ownership_ids"] == [] and change["steps"] == []
    assert change["warnings"] == [
        {"row": "16", "text": "Layer sum and reference width do not match.", "blocking": True},
        {"row": "9007199254740993", "text": "Revit did not return an independent nominal type width.", "blocking": True},
    ]


def test_reader_uses_only_in_use_types_and_independent_widths(monkeypatch):
    monkeypatch.syspath_prepend(str(READER.parent))

    class Id:
        def __init__(self, value):
            self.Value = int(value)

    class Wall:
        def GetTypeId(self):
            return Id(50)

    class Floor:
        def GetTypeId(self):
            return Id(60)

    class Material:
        Name = "Material A"
        UniqueId = "mat-one"

    class WallType:
        Name = "Wall A"
        UniqueId = "wall-one"
        Width = 0.5

        def GetCompoundStructure(self):
            return structure

    class FloorType:
        Name = "Floor A"
        UniqueId = "floor-one"

        def GetCompoundStructure(self):
            return structure

        def get_Parameter(self, parameter):
            assert parameter == "default-thickness"
            return NS(StorageType="Double", HasValue=True, AsDouble=lambda: 0.75)

    source_layer = NS(MaterialId=Id(90), Function="Structure", Width=0.5)
    structure = NS(LayerCount=1, GetLayers=lambda: [source_layer],
                   IsVerticallyCompound=False, VariableLayerIndex=-1)

    class Collector:
        def __init__(self, _document):
            pass

        def OfClass(self, cls):
            self.cls = cls
            return self

        def WhereElementIsNotElementType(self):
            return [Wall(), Wall()] if self.cls is Wall else [Floor()]

    db = NS(Wall=Wall, Floor=Floor, WallType=WallType, FloorType=FloorType, Material=Material, ElementId=Id,
            FilteredElementCollector=Collector, StorageType=NS(Double="Double"),
            BuiltInParameter=NS(FLOOR_ATTR_DEFAULT_THICKNESS_PARAM="default-thickness"),
            Element=NS(Name=NS(GetValue=lambda item: item.Name)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    elements = {50: WallType(), 60: FloorType(), 70: FloorType(), 90: Material()}
    doc = NS(IsFamilyDocument=False, PathName="Synthetic.rvt", ProjectInformation=NS(UniqueId="test"),
             GetElement=lambda identifier: elements.get(identifier.Value))
    result = runpy.run_path(str(READER), init_globals={"doc": doc, "inputs": {}})["result"]
    assert result["status"] == "ok", result
    rows = json.loads(result["snapshot_json"])["types"]
    assert [(row["id"], row["instances"], row["width_ft"]) for row in rows] == [
        ("50", 2, 0.5), ("60", 1, 0.75),
    ]
    assert rows[0]["layers"][0] == {"material_id": "90", "material_uid": "mat-one", "material": "Material A",
                                    "function": "Structure", "width_ft": 0.5, "variable": False}


def test_creation_uses_view_plane_and_rolls_back_on_late_failure(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(READER.parent))
    import hashlib
    import math

    import compound_legend_preview as preview

    class Id:
        def __init__(self, value):
            self.Value = value

    class XYZ:
        def __init__(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z

        def Add(self, other):
            return XYZ(self.X + other.X, self.Y + other.Y, self.Z + other.Z)

        def Subtract(self, other):
            return self.Add(other.Multiply(-1))

        def Multiply(self, value):
            return XYZ(self.X * value, self.Y * value, self.Z * value)

        def DotProduct(self, other):
            return self.X * other.X + self.Y * other.Y + self.Z * other.Z

        def DistanceTo(self, other):
            difference = self.Subtract(other)
            return math.sqrt(difference.DotProduct(difference))

    view_id, no_template = Id(20), Id(-1)

    class View:
        Id, UniqueId = view_id, "view-new"
        Origin, RightDirection, UpDirection = XYZ(10, 20, 5), XYZ(0, 1, 0), XYZ(-1, 0, 0)
        IsTemplate, ViewTemplateId = False, no_template
        Scale = 1

        @classmethod
        def Create(cls, doc, type_id):
            view = cls()
            view.GetTypeId = lambda: type_id
            doc.items[20] = view
            return view

    class TextNote:
        @classmethod
        def Create(cls, doc, owner_id, point, text, options):
            note = cls()
            note.Id = Id(len(doc.items) + 30)
            note.UniqueId = "note-" + str(note.Id.Value)
            note.OwnerViewId, note.position, note.Text, note.type_id = owner_id, point, text, options
            doc.items[note.Id.Value] = note
            return note

        def GetTypeId(self):
            return self.type_id

        def get_BoundingBox(self, _view):
            frame = NS(OfPoint=lambda p: self.position.Add(View.RightDirection.Multiply(p.X))
                       .Add(View.UpDirection.Multiply(p.Y)))
            return NS(Min=XYZ(0, -0.75, 0), Max=XYZ(4, 0, 0), Transform=frame)

    class DetailCurve:
        def __init__(self, doc, owner, geometry):
            self.Id = Id(len(doc.items) + 30)
            self.UniqueId = "curve-" + str(self.Id.Value)
            self.OwnerViewId, self.GeometryCurve = owner, geometry

    class Collector:
        def __init__(self, doc):
            self.doc = doc

        def OwnedByView(self, owner):
            self.owner = owner
            return self

        def WhereElementIsNotElementType(self):
            return [item for item in self.doc.items.values()
                    if getattr(item, "OwnerViewId", None) is self.owner]

    status = NS(Started="Started", Committed="Committed", RolledBack="RolledBack")
    events = []

    class Transaction:
        def __init__(self, doc, _title):
            self.doc = doc

        def Start(self):
            events.append("start")
            return status.Started

        def GetStatus(self):
            return status.Started

        def RollBack(self):
            self.doc.items.clear()
            events.append("rollback")
            return status.RolledBack

        def Commit(self):
            events.append("commit")
            return status.Committed

    move_fault = None

    def move(doc, element_id, direction):
        note = doc.items[element_id.Value]
        note.position = note.position.Add(direction)
        if move_fault == "crowd":
            note.position = note.position.Subtract(View.RightDirection.Multiply(3))
        if move_fault == "overlap":
            note.position = note.position.Add(View.UpDirection.Multiply(direction.DotProduct(View.UpDirection) * -1 - 1))

    db = NS(ElementId=Id, XYZ=XYZ, ViewDrafting=View, TextNote=TextNote, DetailCurve=DetailCurve,
            FilteredElementCollector=Collector, TextNoteOptions=lambda element_id: element_id,
            ElementTransformUtils=NS(MoveElement=move),
            Line=NS(CreateBound=lambda a, b: NS(GetEndPoint=lambda i: (a, b)[i])),
            Transaction=Transaction, TransactionStatus=status,
            Element=NS(Name=NS(GetValue=lambda item: item.Name)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))

    import read_compound_legend as source
    project = {"uid": "project-test", "path_sha256": hashlib.sha256(b"Synthetic.rvt").hexdigest()}
    request = {"view_name": "Layer legend", "drafting_type_id": "5", "text_type_id": "6"}
    fixture = {"project": project, "request": request, "target": {"name": "Layer legend"},
               "types": [{"category": "Wall", "name": "Wall A", "id": "11", "instances": 2, "width_ft": 0.5,
                          "layers": [layer(0.25), layer(0), layer(0.25)]},
                         {"category": "Floor", "name": "Floor A", "id": "12", "instances": 1, "width_ft": 0.5,
                          "layers": [layer(0.5)]}]}
    monkeypatch.setattr(source, "read_snapshot", lambda _doc, _request: fixture)
    doc = NS(items={}, PathName="Synthetic.rvt", ProjectInformation=NS(UniqueId="project-test"),
             IsReadOnly=False, Application=NS(ShortCurveTolerance=0.003), Regenerate=lambda: None)
    doc.GetElement = lambda element_id: doc.items.get(element_id.Value)
    fail = True
    calls = []

    def new_curve(view, geometry):
        calls.append(geometry)
        assert geometry.GetEndPoint(0).Z == 5
        if fail and len(calls) == 2:
            return None
        curve = DetailCurve(doc, view.Id, geometry)
        doc.items[curve.Id.Value] = curve
        return curve

    doc.Create = NS(NewDetailCurve=new_curve)
    approved = preview.compute(fixture)
    script = READER.with_name("create_compound_legend.py")

    def run(args):
        return runpy.run_path(str(script), init_globals={"doc": doc, "inputs": args})["result"]

    blocked = {"mode": "apply", **approved, "preview": dict(approved["preview"], blocked=True)}
    assert run(blocked)["status"] == "stopped"
    assert not events
    fixture["types"][0]["instances"] = 3
    assert run({"mode": "apply", **approved})["status"] == "stopped"
    assert not events
    fixture["types"][0]["instances"] = 2

    rolled = run({"mode": "apply", **approved})
    assert rolled == {"status": "stopped", "message": "Revit could not create a section line."}
    assert events == ["start", "rollback"] and doc.items == {}

    fail = False
    move_fault = "crowd"
    crowded = run({"mode": "apply", **approved})
    assert crowded == {"status": "stopped", "message": "A layer table overlaps or crowds its section sketch."}
    assert events[-2:] == ["start", "rollback"] and doc.items == {}
    move_fault = "overlap"
    overlapping = run({"mode": "apply", **approved})
    assert overlapping == {"status": "stopped", "message": "The generated legend rows overlap."}
    assert events[-2:] == ["start", "rollback"] and doc.items == {}
    move_fault = None
    kept = run({"mode": "apply", **approved})
    assert kept["status"] == "ok", kept
    assert events[-2:] == ["start", "commit"]
    receipt = json.loads(kept["legend_json"])
    assert receipt["view_id"] == "20" and receipt["view_uid"] == "view-new"
    assert receipt["scale"] == preview.VIEW_SCALE
    verified = run({"mode": "verify", "view_id": "20", "view_uid": "view-new"})
    assert verified == kept
    ids = sorted(preview.verify_contents(approved["plan"], receipt))
    dry = {"result": kept, "changes": {"added_ids": ids, "added_count": len(ids),
                                       "deleted_count": 0, "modified_count": 0}}
    preview.verify_dry_run(approved["plan"], dry)
    dry["changes"]["modified_count"] = 1
    import pytest
    with pytest.raises(ValueError, match="only the reviewed view"):
        preview.verify_dry_run(approved["plan"], dry)

    dry["changes"]["modified_count"] = 0
    artifact_path = tmp_path / "legend.json"
    artifact_path.write_text(json.dumps(approved))
    check_records = {
        "baseline": {"status": "ok", "snapshot_json": json.dumps(fixture)},
        "side-effects": dry,
        "compare": {"result": verified},
    }
    kept_path = tmp_path / "kept.json"
    kept_path.write_text(json.dumps({"result": kept}))
    for check, record in check_records.items():
        path = tmp_path / (check + ".json")
        path.write_text(json.dumps(record))
        result = subprocess.run([sys.executable, SCRIPT, path, artifact_path, "--check", check,
                                 "--kept-receipt", kept_path], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
