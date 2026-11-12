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
    Category = NS(Id=NS(Value=-1))
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
    symbols[4].Category = NS(Id=NS(Value=-2))
    instances = {-1: [Instance(large_id, width=length(3, "3 ft")),
                      Instance(large_id, width=length(3.5, "3.5 ft"))],
                 -2: [Instance(4, width=length(2, "2 ft"))]}
    resolved = []
    disposed = []

    class View:
        Id = NS(Value=25)
        UniqueId = "fixture-legend"
        Name = "Fixture legend"
        IsTemplate = False
        ViewType = "Legend"
        Scale = 20
        Origin = NS(X=0, Y=0, Z=0)
        RightDirection = NS(X=1, Y=0, Z=0)
        UpDirection = NS(X=0, Y=1, Z=0)

    view = View()

    def component(key, type_id, direction):
        element = Element({
            "reference": NS(StorageType="ElementId", HasValue=True, IsReadOnly=False,
                            AsElementId=lambda: NS(Value=type_id)),
            "direction": NS(StorageType="Integer", HasValue=True, IsReadOnly=False,
                            AsInteger=lambda: direction),
        })
        element.Id, element.UniqueId = NS(Value=key), "fixture-component-" + str(key)
        element.OwnerViewId, element.Pinned = view.Id, False
        element.get_BoundingBox = lambda _view: None
        return element

    instances[-3] = [component(31, large_id, -7), component(32, large_id, -6), component(33, 9, -7)]
    instances[-4] = [NS(Id=NS(Value=34), UniqueId="fixture-note", OwnerViewId=view.Id,
                        Text="Fixture note", Coord=NS(X=1, Y=2, Z=0), Pinned=False,
                        GetTypeId=lambda: NS(Value=15), get_BoundingBox=lambda _view: None)]

    def get_element(key):
        resolved.append(key.Value)
        return view if key.Value == 25 else symbols.get(key.Value)

    document = NS(IsFamilyDocument=False, IsLinked=False, PathName="synthetic.rvt",
                  ProjectInformation=NS(UniqueId="fixture-host"), GetElement=get_element,
                  Application=NS(VersionNumber="2026"))

    class Collector:
        def __init__(self, doc):
            assert doc is document

        def OwnedByView(self, identifier):
            assert identifier.Value == 25
            return self

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

    db = NS(BuiltInCategory=NS(OST_Doors=-1, OST_Windows=-2, OST_LegendComponents=-3, OST_TextNotes=-4),
            FamilyInstance=Instance, ElementId=lambda value: NS(Value=value),
            View=View, ViewType=NS(Legend="Legend"),
            FamilySymbol=Symbol, FilteredElementCollector=Collector,
            BuiltInParameter=NS(ALL_MODEL_TYPE_MARK="mark", DOOR_WIDTH="width", DOOR_HEIGHT="height",
                                WINDOW_WIDTH="width", WINDOW_HEIGHT="height", LEGEND_COMPONENT="reference",
                                LEGEND_COMPONENT_VIEW="direction", LEGEND_COMPONENT_LENGTH="length"),
            StorageType=NS(String="String", Double="Double", ElementId="ElementId", Integer="Integer"))
    clock = NS(ElapsedMilliseconds=0)
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: clock)))

    def query(**inputs):
        return runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": inputs})["result"]

    query.document = document
    query.db = db

    return query, large_id, instances, resolved, disposed, clock


def test_counts_placed_types_once_and_retains_distinct_shared_marks(fixture_project):
    query, large_id, _, resolved, disposed, _ = fixture_project
    response = query()
    assert response["status"] == "ok" and response["snapshot_json"].isascii()
    result = json.loads(response["snapshot_json"])
    assert result["scope"] == "host-project" and "synthetic.rvt" not in response["snapshot_json"]
    assert result["placements"] == {"Doors": 2, "Windows": 1}
    door, window = result["types"]
    assert resolved == [large_id, 4] and disposed == [-1, -2]
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
    instances[-2].append(Instance(99))
    assert query() == {"status": "stopped", "message": "A placed opening type could not be identified."}
    assert disposed == [-1, -2]
    clock.ElapsedMilliseconds = 10000
    assert query() == {"status": "stopped", "message": "The placed opening inventory exceeded its limits."}


def test_legend_classifies_missing_duplicate_and_unplaced_elevations(fixture_project):
    query, large_id, instances, _, disposed, _ = fixture_project
    response = query(legend_id="25")
    assert response["status"] == "ok"
    legend = json.loads(response["snapshot_json"])["legend"]
    assert legend["coverage"] == {"missing_type_ids": ["4"], "unresolved_type_ids": [],
                                 "duplicate_elevation_type_ids": [str(large_id)],
                                 "unplaced_elevation_type_ids": ["9"], "unknown_direction_component_ids": []}
    assert [row["direction"] for row in legend["components"]] == [
        "front_elevation", "back_elevation", "front_elevation",
    ]
    assert legend["text_notes"][0]["text"] == "Fixture note"
    assert disposed == [-1, -2, -3, -4]
    instances[-3][2].parameters["reference"].AsElementId = lambda: NS(Value=4)
    instances[-3][2].parameters["direction"].AsInteger = lambda: -100
    unresolved = json.loads(query(legend_id="25")["snapshot_json"])["legend"]["coverage"]
    assert unresolved["missing_type_ids"] == []
    assert unresolved["unresolved_type_ids"] == ["4"]
    assert unresolved["unknown_direction_component_ids"] == ["33"]
    instances[-3][2].parameters["direction"].AsInteger = lambda: -7
    query.document.Application.VersionNumber = "2025"
    legend = json.loads(query(legend_id="25")["snapshot_json"])["legend"]
    assert legend["coverage"] is None
    assert [(row["direction_raw"], row["direction"]) for row in legend["components"]] == [
        (-7, None), (-6, None), (-7, None),
    ]
    assert query(legend_id="26")["status"] == "stopped"


def test_layout_reads_seed_target_graphics_and_both_sheet_placements(fixture_project, monkeypatch):
    query, _, instances, _, _, _ = fixture_project
    db, doc = query.db, query.document
    original_lookup = doc.GetElement
    original_collector = db.FilteredElementCollector
    target = original_lookup(NS(Value=25))
    xyz = lambda x, y, z=0: NS(X=x, Y=y, Z=z)

    def graphic(item, owner, category, kind):
        item.OwnerViewId = owner.Id
        item.Category = NS(Id=NS(Value=category))
        item.GroupId = NS(Value=-1)
        item.Pinned = False
        item.GetType = lambda: NS(Name=kind)
        item.get_BoundingBox = lambda _view: NS(Min=xyz(0, 0), Max=xyz(3, 4), Dispose=lambda: None)
        return item

    source = db.View()
    source.Id, source.UniqueId = NS(Value=26), "fixture-source-legend"
    seed = graphic(Element(dict(instances[-3][0].parameters)), source, -3, "Element")
    seed.Id, seed.UniqueId = NS(Value=60), "fixture-seed"

    class TextType:
        UniqueId = "fixture-text-type"
        Name = "Fixture text type"

    class Sheet:
        def __init__(self, key):
            self.Id, self.UniqueId, self.SheetNumber = NS(Value=key), "fixture-sheet-" + str(key), "A" + str(key)
            self.Outline = NS(Min=NS(U=0, V=0), Max=NS(U=3, V=2), Dispose=lambda: None)

    class Viewport:
        def __init__(self, key, sheet, view, width):
            self.Id, self.UniqueId = NS(Value=key), "fixture-viewport-" + str(key)
            self.SheetId, self.ViewId = sheet.Id, view.Id
            self.Rotation = "None"
            self.width = width
            graphic(self, sheet, -5, "Viewport")

        def GetBoxOutline(self):
            return NS(IsEmpty=False, MinimumPoint=xyz(0, 0), MaximumPoint=xyz(self.width, 1), Dispose=lambda: None)

        def GetLabelOutline(self):
            return NS(IsEmpty=False, MinimumPoint=xyz(0, -0.2), MaximumPoint=xyz(1, 0), Dispose=lambda: None)

    a, b = Sheet(81), Sheet(82)
    vp_a, vp_b, adjacent = Viewport(71, a, target, 1), Viewport(72, b, target, 2), Viewport(73, b, source, 1)
    items = {26: source, 60: seed, 70: TextType(), 81: a, 82: b}
    for item in instances[-3] + instances[-4]:
        category = -3 if item in instances[-3] else -4
        graphic(item, target, category, "Element" if category == -3 else "TextNote")
    manual = graphic(NS(Id=NS(Value=36), UniqueId="fixture-line"), target, -6, "DetailLine")
    owned = {25: instances[-3] + instances[-4] + [manual], 26: [seed], 81: [vp_a], 82: [vp_b, adjacent]}
    doc.GetElement = lambda identifier: items.get(identifier.Value) or original_lookup(identifier)
    db.Viewport, db.ViewSheet, db.TextNoteType, db.XYZ = Viewport, Sheet, TextType, xyz

    class Collector(original_collector):
        owner = category = kind = None

        def OwnedByView(self, identifier):
            self.owner = identifier.Value
            return self

        def OfClass(self, kind):
            self.kind = kind
            return self

        def WhereElementIsNotElementType(self):
            if self.owner is not None:
                return [row for row in owned[self.owner] if self.category is None or row.Category.Id.Value == self.category]
            if self.kind is Viewport:
                return [vp_a, vp_b, adjacent]
            return super().WhereElementIsNotElementType()

    monkeypatch.setattr(db, "FilteredElementCollector", Collector)
    result = query(legend_id="25", source_legend_id="26", seed_component_id="60", text_type_id="70")
    assert result["status"] == "ok", result
    layout = json.loads(result["snapshot_json"])["legend"]["layout"]
    assert layout["seed"]["id"] == "60" and layout["seed"]["direction"] == "front_elevation"
    assert layout["text_type"]["uid"] == "fixture-text-type"
    assert layout["front_elevation_coverage"]["duplicate_elevation_type_ids"] == []
    assert [item["id"] for item in layout["target_graphics"]] == ["31", "32", "33", "34", "36"]
    assert [(sheet["id"], sheet["legend_viewport_ids"]) for sheet in layout["sheet_placements"]] == [
        ("81", ["71"]), ("82", ["72"]),
    ]
    second = layout["sheet_placements"][1]
    assert [item["view_id"] for item in second["graphics"]] == ["25", "26"]
    assert second["graphics"][0]["label_bounds_ft"] == {"min": [0, -0.2, 0], "max": [1, 0, 0]}
    owned[82].remove(vp_b)
    rejected = query(legend_id="25", source_legend_id="26", seed_component_id="60", text_type_id="70")
    assert rejected == {"status": "stopped", "message": "The legend's sheet viewport evidence could not be reconciled."}
