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
    db = NS(View=View, ElementId=MockId, FilterElement=Filter, LinePatternElement=NS(GetSolidPatternId=lambda: MockId(-300001)))
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
    settings[20].IsSurfaceForegroundPatternVisible = True
    view = View(settings, temporary)
    elements = {10: Filter(10), 20: Filter(20), 31: view}
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
