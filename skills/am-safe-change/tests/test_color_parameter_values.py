import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class ElementId:
    def __init__(self, value):
        self.Value = int(value)


class NativeList:
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self):
        self.values = []

    def Add(self, item):
        self.values.append(item.Value)


class View:
    Id = ElementId(31)
    UniqueId = "view-31"
    IsTemplate = False

    def AreGraphicsOverridesAllowed(self):
        return True

    def IsTemporaryViewPropertiesModeEnabled(self):
        return False


class Link:
    Id = ElementId(99)


class Collector:
    def __init__(self, document, view_id):
        assert view_id.Value == 31
        self.document = document

    @staticmethod
    def IsViewValidForElementIteration(_document, view_id):
        return view_id.Value == 31

    def WhereElementIsNotElementType(self):
        return self

    def WherePasses(self, filters):
        assert filters.ids == [-200]
        return self.document.candidates

    def OfClass(self, kind):
        assert kind is Link
        return [Link()]

    def Dispose(self):
        self.document.collectors_closed += 1


@pytest.fixture
def reader(monkeypatch):
    def bounded(items, limit, label, clock):
        for index, value in enumerate(items):
            if index >= limit or clock.ElapsedMilliseconds > 10000:
                raise ValueError(label)
            yield value

    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=NativeList))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=1))))
    db = NS(ElementId=ElementId, View=View, RevitLinkInstance=Link, FilteredElementCollector=Collector,
            ElementMulticategoryFilter=lambda ids: NS(ids=ids.values, Dispose=lambda: None),
            ParameterFilterUtilities=NS(GetFilterableParametersInCommon=lambda _doc, _cats: [ElementId(-55)]))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "duplicate_views", NS(
        bounded=bounded, document_identity=lambda _doc: {"project_uid": "project-1"},
        identifier=ElementId, key=lambda value: str(value.Value)))
    monkeypatch.setitem(sys.modules, "read_view_filter_graphics", NS(native_number=float))
    return runpy.run_path(str(SCRIPTS / "read_color_parameter_values.py"))


def parameter(storage, value, has_value=True, param_id=-55):
    return NS(Id=ElementId(param_id), IsShared=False, HasValue=has_value, StorageType=storage,
              Definition=NS(GetDataType=lambda: NS(TypeId="spec-" + storage)),
              AsString=lambda: value, AsInteger=lambda: value, AsDouble=lambda: value,
              AsElementId=lambda: ElementId(value), AsValueString=lambda: "label")


def element(identifier, parameters, type_id=-1):
    return NS(Id=ElementId(identifier), UniqueId="element-%d" % identifier, Parameters=parameters,
              Category=NS(Id=ElementId(-200)), GetTypeId=lambda: ElementId(type_id))


def test_native_inventory_separates_inherited_empty_unset_missing_and_special_values(reader):
    values = {
        31: View(),
        101: element(101, [parameter("String", "ignored", param_id=-22), parameter("String", None, False)], 501),
        102: element(102, [parameter("String", "")], 501),
        103: element(103, [parameter("String", None, False)]),
        104: element(104, []),
        105: element(105, [parameter("ElementId", -2000000)]),
        106: element(106, [], 501),
        501: element(501, [parameter("String", "type value")]),
    }
    document = NS(GetElement=lambda identifier: values.get(identifier.Value), collectors_closed=0,
                  candidates=[values[index] for index in range(101, 107)])
    result = reader["main"](document, {"view_id": "31", "parameter_id": "-55", "category_ids": ["-200"]})
    inventory = json.loads(result["inventory_json"])
    assert inventory["scope"] == "potentially_visible_host_elements"
    assert document.collectors_closed == 2
    assert [row["source"] for row in inventory["rows"]] == ["type", "instance", "unset", "missing", "instance", "type"]
    assert inventory["rows"][1]["instance"]["value"] == ""
    assert inventory["rows"][4]["instance"]["value"] == "-2000000"
    assert inventory["types"]["501"]["uid"] == "element-501"
    assert inventory["types"]["501"]["parameter"]["value"] == "type value"
    assert inventory["missing_parameter_ids"] == ["104"] and inventory["link_candidate_ids"] == ["99"]


def test_reader_rejects_ambiguous_parameter_id_and_unfilterable_selection(reader):
    row = element(101, [parameter("String", "one"), parameter("String", "two")])
    document = NS(GetElement=lambda _id: View(), collectors_closed=0, candidates=[row])
    with pytest.raises(ValueError, match="ambiguous"):
        reader["main"](document, {"view_id": "31", "parameter_id": "-55", "category_ids": ["-200"]})
    assert document.collectors_closed == 1
    with pytest.raises(ValueError, match="not filterable"):
        reader["main"](document, {"view_id": "31", "parameter_id": "-56", "category_ids": ["-200"]})
    assert document.collectors_closed == 1
