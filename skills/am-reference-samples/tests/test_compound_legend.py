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
