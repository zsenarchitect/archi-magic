import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_opening_types.py"


def length(value, label):
    return NS(StorageType="Double", HasValue=True, AsDouble=lambda: value, AsValueString=lambda: label)


class Element:
    def __init__(self, parameters):
        self.parameters = parameters

    def get_Parameter(self, key):
        return self.parameters.get(key)


class Symbol(Element):
    FamilyName = "Fixture family"
    Name = "Ouverture spéciale"

    def __init__(self, key, **parameters):
        super().__init__(parameters)
        self.UniqueId = "fixture-type-" + str(key)


class Instance(Element):
    def __init__(self, key, **parameters):
        super().__init__(parameters)
        self.key = key

    def GetTypeId(self):
        return NS(Value=self.key)


@pytest.fixture
def fixture_project(monkeypatch):
    large_id = 9007199254740993
    mark = NS(StorageType="String", HasValue=True, AsString=lambda: "A–01")
    symbols = {
        large_id: Symbol(large_id, mark=mark, width=length(3, "3 ft"), height=length(7, "7 ft")),
        4: Symbol(4, mark=mark, height=length(4, "4 ft")),
        9: Symbol(9, mark=mark),
    }
    instances = {"doors": [Instance(large_id, width=length(3, "3 ft")),
                           Instance(large_id, width=length(3.5, "3.5 ft"))],
                 "windows": [Instance(4, width=length(2, "2 ft"))]}
    resolved = []
    disposed = []

    def get_element(key):
        resolved.append(key.Value)
        return symbols.get(key.Value)

    document = NS(IsFamilyDocument=False, IsLinked=False, PathName="synthetic.rvt",
                  ProjectInformation=NS(UniqueId="fixture-host"), GetElement=get_element)

    class Collector:
        def __init__(self, doc):
            assert doc is document

        def OfCategory(self, category):
            self.category = category
            return self

        def OfClass(self, kind):
            assert kind is Instance
            return self

        def WhereElementIsNotElementType(self):
            return instances[self.category]

        def Dispose(self):
            disposed.append(self.category)

    db = NS(BuiltInCategory=NS(OST_Doors="doors", OST_Windows="windows"), FamilyInstance=Instance,
            FamilySymbol=Symbol, FilteredElementCollector=Collector,
            BuiltInParameter=NS(ALL_MODEL_TYPE_MARK="mark", DOOR_WIDTH="width", DOOR_HEIGHT="height",
                                WINDOW_WIDTH="width", WINDOW_HEIGHT="height"),
            StorageType=NS(String="String", Double="Double"))
    clock = NS(ElapsedMilliseconds=0)
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: clock)))

    def query():
        return runpy.run_path(str(SCRIPT), init_globals={"doc": document})["result"]

    return query, large_id, instances, resolved, disposed, clock


def test_counts_placed_types_once_and_retains_distinct_shared_marks(fixture_project):
    query, large_id, _, resolved, disposed, _ = fixture_project
    response = query()
    assert response["status"] == "ok" and response["snapshot_json"].isascii()
    result = json.loads(response["snapshot_json"])
    assert result["scope"] == "host-project" and "synthetic.rvt" not in response["snapshot_json"]
    assert result["placements"] == {"Doors": 2, "Windows": 1}
    door, window = result["types"]
    assert resolved == [large_id, 4] and disposed == ["doors", "windows"]
    assert door["id"] == str(large_id) and door["placements"] == 2
    assert door["type"] == "Ouverture spéciale" and door["type_mark"] == window["type_mark"] == "A–01"
    assert door["width"] == {"feet": 3.0, "display": "3 ft"}
    assert door["instance_dimension_counts"]["width"] == 2
    assert door["instance_dimension_ranges_ft"]["width"] == [3.0, 3.5]
    assert door["instance_dimensions_vary"] == {"width": True, "height": False}
    assert window["width"] is None and window["height"] == {"feet": 4.0, "display": "4 ft"}
    assert window["instance_dimension_ranges_ft"]["width"] == [2.0, 2.0]


def test_refuses_missing_type_or_read_timeout_without_partial_inventory(fixture_project):
    query, _, instances, _, disposed, clock = fixture_project
    instances["windows"].append(Instance(99))
    assert query() == {"status": "stopped", "message": "A placed opening type could not be identified."}
    assert disposed == ["doors", "windows"]
    clock.ElapsedMilliseconds = 10000
    assert query() == {"status": "stopped", "message": "The placed opening inventory exceeded its limits."}
