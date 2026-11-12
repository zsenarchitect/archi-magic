import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class MockId:
    def __init__(self, value):
        self.Value = int(value)


class Filter:
    def __init__(self, value):
        self.Name = "Filter %d" % value
        self.UniqueId = "filter-%d" % value


class SelectionFilter(Filter):
    def GetElementIds(self):
        return [MockId(502), MockId(501)]


class ParameterFilter(Filter):
    def __init__(self, value):
        super().__init__(value)
        self.numeric = NS(
            GetType=lambda: NS(Name="FilterDoubleRule"),
            GetRuleParameter=lambda: MockId(-15),
            GetEvaluator=lambda: NS(GetType=lambda: NS(FullName="Autodesk.Revit.DB.FilterNumericGreater")),
            RuleValue=4.25, Epsilon=0.01, Dispose=lambda: None,
        )
        self.rule = NS(GetType=lambda: NS(Name="FilterInverseRule"), GetRuleParameter=lambda: MockId(-15),
                       GetInnerRule=lambda: self.numeric, Dispose=lambda: None)
        self.expression = NS(GetType=lambda: NS(Name="ElementParameterFilter"), Inverted=False,
                             GetRules=lambda: [self.rule], Dispose=lambda: None)

    def GetCategories(self):
        return [MockId(-300), MockId(-400)]

    def GetElementFilter(self):
        return self.expression


class FillPattern:
    UniqueId = "pattern-68"

    def __init__(self):
        self.grid = NS(Angle=0.125, Offset=0.5, Shift=0.0, Origin=NS(U=0.25, V=0.0),
                       GetSegments=lambda: [0.3, -0.2, 0.0], Dispose=lambda: None)

    def GetFillPattern(self):
        return NS(Name="Drafting pattern", Target="Drafting", HostOrientation="ToView", IsSolidFill=False,
                  GetFillGrids=lambda: [self.grid], Dispose=lambda: None)


class LinePattern:
    @staticmethod
    def GetSolidPatternId():
        return MockId(-300001)

    @staticmethod
    def GetLinePattern(document, identifier):
        assert identifier.Value == -300009
        return NS(Name="Built-in dashed", GetSegments=lambda: [NS(Type="Dash", Length=0.2),
                                                               NS(Type="Space", Length=0.1)],
                  Dispose=lambda: None)


class View:
    Id = MockId(31)
    Name = "Review"
    UniqueId = "view-31"
    ViewTemplateId = MockId(41)
    IsTemplate = False

    def __init__(self, settings, temporary):
        self.settings = settings
        self.temporary = temporary

    def AreGraphicsOverridesAllowed(self):
        return True

    def IsTemporaryViewPropertiesModeEnabled(self):
        return self.temporary

    def GetOrderedFilters(self):
        return [MockId(20), MockId(10)]

    def GetFilterOverrides(self, item):
        return self.settings[item.Value]

    def GetFilterVisibility(self, item):
        return item.Value == 10

    def GetIsFilterEnabled(self, item):
        return item.Value == 20


@pytest.fixture
def reader(monkeypatch):
    db = NS(View=View, ElementId=MockId, FilterElement=Filter, LinePatternElement=LinePattern,
            SelectionFilterElement=SelectionFilter, ParameterFilterElement=ParameterFilter,
            FillPatternElement=FillPattern)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=1))))
    helpers = runpy.run_path(str(SCRIPTS / "duplicate_views.py"))
    monkeypatch.setitem(sys.modules, "duplicate_views", NS(**helpers))
    return runpy.run_path(str(SCRIPTS / "read_view_filter_graphics.py"))


def view_fixture(reader, temporary=False):
    disposed = []

    def make_settings(identifier):
        fields = {name: NS(IsValid=False) for name in reader["COLORS"]}
        fields.update({name: MockId(-1) for name in reader["PATTERNS"]})
        fields.update({name: False for name in reader["SWITCHES"]})
        return NS(**fields, IsValidObject=True, CutLineWeight=-1, ProjectionLineWeight=3,
                  Transparency=0, DetailLevel="Undefined", Dispose=lambda: disposed.append(identifier))

    settings = {value: make_settings(value) for value in (10, 20)}
    settings[20].CutLineColor = NS(IsValid=True, Red=0, Green=0, Blue=0)
    settings[20].CutLinePatternId = MockId(-300001)
    settings[20].SurfaceForegroundPatternId = MockId(68)
    settings[10].ProjectionLinePatternId = MockId(-300009)
    settings[20].IsSurfaceForegroundPatternVisible = True
    view = View(settings, temporary)
    elements = {10: SelectionFilter(10), 20: ParameterFilter(20), 31: view, 68: FillPattern()}
    document = NS(IsFamilyDocument=False, IsReadOnly=False, PathName="fixture.rvt",
                  ProjectInformation=NS(UniqueId="project-1"),
                  GetElement=lambda identifier: elements.get(identifier.Value))
    return document, disposed


def test_native_inventory_preserves_order_flags_and_graphic_sentinels(reader):
    document, disposed = view_fixture(reader)
    result = reader["main"](document, {"view_ids": ["31"]})
    snapshot = json.loads(result["inventory_json"])
    view = snapshot["views"][0]
    assert view["template_id"] == "41"
    assert [(row["id"], row["visible"], row["enabled"]) for row in view["filters"]] == [
        ("20", False, True), ("10", True, False),
    ]
    assert disposed == [20, 10]
    actual = view["filters"][0]["graphics"]
    assert actual["CutLineColor"] == [0, 0, 0] and actual["ProjectionLineColor"] is None
    assert actual["CutLinePatternId"] == {"kind": "solid_line"}
    assert actual["SurfaceForegroundPatternId"] == {"kind": "element", "id": "68"}
    assert actual["ProjectionLinePatternId"] is None and actual["Transparency"] == 0
    assert actual["IsSurfaceForegroundPatternVisible"] is True


def test_temporary_graphics_are_rejected_before_reading_overrides(reader):
    document, disposed = view_fixture(reader, temporary=True)
    with pytest.raises(ValueError, match="Temporary View Properties"):
        reader["main"](document, {"view_ids": ["31"]})
    assert disposed == []


def test_rules_and_patterns_change_snapshot_without_id_changes(reader):
    document, _ = view_fixture(reader)

    def inventory():
        return json.loads(reader["main"](document, {"view_ids": ["31"]})["inventory_json"])

    first = inventory()
    assert first["filter_definitions"] == {
        "10": {"kind": "selection", "members": ["501", "502"]},
        "20": {"kind": "parameter", "categories": ["-400", "-300"],
               "tree": {"type": "ElementParameterFilter", "inverted": False, "rules": [
                   {"type": "FilterInverseRule", "parameter": "-15", "inner": {
                       "type": "FilterDoubleRule", "parameter": "-15",
                       "evaluator": "Autodesk.Revit.DB.FilterNumericGreater", "value": 4.25, "epsilon": 0.01,
                   }},
               ]}},
    }
    assert first["pattern_definitions"] == {
        "-300009": {"kind": "built_in_line", "uid": None, "name": "Built-in dashed",
                    "segments": [["Dash", 0.2], ["Space", 0.1]]},
        "68": {"kind": "fill", "uid": "pattern-68", "name": "Drafting pattern", "target": "Drafting",
               "orientation": "ToView", "solid": False, "grids": [
                   {"angle": 0.125, "offset": 0.5, "shift": 0.0, "origin": [0.25, 0.0],
                    "segments": [0.3, -0.2, 0.0]},
               ]},
    }

    document.GetElement(MockId(20)).numeric.Epsilon = 0.02
    rule_changed = inventory()
    assert rule_changed["views"] == first["views"]
    assert rule_changed["filter_definitions"] != first["filter_definitions"]
    assert rule_changed["pattern_definitions"] == first["pattern_definitions"]
    document.GetElement(MockId(68)).grid.Offset = 0.75
    pattern_changed = inventory()
    assert pattern_changed["views"] == first["views"]
    assert pattern_changed["pattern_definitions"] != first["pattern_definitions"]


def test_unknown_rules_and_oversize_selection_stop_inventory(reader):
    document, _ = view_fixture(reader)
    document.GetElement(MockId(20)).numeric.GetType = lambda: NS(Name="UnknownNativeRule")
    with pytest.raises(ValueError, match="Unsupported native filter rule"):
        reader["main"](document, {"view_ids": ["31"]})
    document.GetElement(MockId(20)).expression = None
    document.GetElement(MockId(10)).GetElementIds = lambda: map(MockId, range(1001))
    with pytest.raises(ValueError, match="filter member inventory exceeds"):
        reader["main"](document, {"view_ids": ["31"]})
