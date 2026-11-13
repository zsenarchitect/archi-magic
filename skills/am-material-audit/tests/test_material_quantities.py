import hashlib
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_material_quantities.py"


def test_query_groups_geometry_and_reports_missing_elements(monkeypatch):
    class ElementId:
        Value = None

        def __init__(self, value):
            self.Value = value

    def element(area, volume, level_id=10):
        return NS(
            Category=NS(Id=ElementId(30), Name="Walls", HasMaterialQuantities=True),
            LevelId=ElementId(level_id),
            GetMaterialIds=lambda paint: [] if paint else [ElementId(100)],
            GetMaterialArea=lambda material_id, paint: 0 if paint else area,
            GetMaterialVolume=lambda material_id: volume,
        )

    elements = {
        1: element(100, 200), 2: element(200, 300),
        3: element(50, 100, level_id=8),
        8: NS(Id=ElementId(8), Name="Level 2"),
        10: NS(Id=ElementId(10), Name="Level 1"),
        100: NS(Name="Material A"),
    }
    fake_db = NS(
        ElementId=ElementId,
        UnitUtils=NS(ConvertFromInternalUnits=lambda value, unit: value * unit),
        UnitTypeId=NS(SquareMeters=0.1, CubicMeters=0.01),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=fake_db))
    monkeypatch.setitem(sys.modules, "System", NS(Int32=int, Int64=int))
    document = NS(IsFamilyDocument=False, GetElement=lambda id_: elements.get(id_.Value),
                  PathName="Synthetic.rvt", ProjectInformation=NS(UniqueId="synthetic-project"))
    execution = runpy.run_path(
        str(SCRIPT), init_globals={"doc": document, "inputs": {"element_ids": [1, 2, 3, 999, 1]}},
    )
    report = json.loads(execution["result"]["payload"])
    assert report["project"] == {"uid": "synthetic-project",
                                 "path_sha256": hashlib.sha256(b"Synthetic.rvt").hexdigest()}
    assert report["requested_elements"] == 4
    assert report["requested_element_ids"] == ["1", "2", "3", "999"]
    assert report["totals_by_level"] == [
        {"level_id": 8, "level": "Level 2", "area_m2": 5.0, "volume_m3": 1.0},
        {"level_id": 10, "level": "Level 1", "area_m2": 30.0, "volume_m3": 5.0},
    ]
    assert report["status"] == "incomplete"
    assert report["issues"] == [{"element_id": 999, "reason": "element_not_found"}]
    assert report["rows"] == [
        {"category": "Walls", "category_id": 30, "level": "Level 2", "level_id": 8,
         "material": "Material A", "material_id": 100, "area_m2": 5.0, "volume_m3": 1.0},
        {"category": "Walls", "category_id": 30, "level": "Level 1", "level_id": 10,
         "material": "Material A", "material_id": 100, "area_m2": 30.0, "volume_m3": 5.0},
    ]

    elements[1].GetMaterialIds = lambda paint: [ElementId(100)] * 5001
    limited = execution["takeoff"](document, [1, 2])
    assert limited["status"] == "incomplete"
    assert limited["issues"] == [{"element_id": 1, "reason": "quantity_read_budget_exceeded"}]
    assert limited["rows"][0]["area_m2"] == 50000.0
