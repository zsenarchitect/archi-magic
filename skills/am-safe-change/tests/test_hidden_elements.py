"""Run the active-view diagnosis, preview and transaction entry points."""
import copy
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import hidden_elements_preview as preview


class Id:
    def __init__(self, value):
        self.Value = value


class Element:
    ViewSpecific = False

    def __init__(self, identifier, category):
        self.Id = Id(identifier)
        self.UniqueId = "element-" + str(identifier)
        self.Category = category

    def IsHidden(self, view):
        return self.Id.Value in view.direct


class SelectionFilter:
    def __init__(self, identifier, values):
        self.UniqueId = "filter-" + str(identifier)
        self.values = values

    def GetElementIds(self):
        return [Id(value) for value in self.values]


class ParameterFilter:
    UniqueId = "parameter-filter"

    def GetCategories(self):
        return [Id(-11)]

    def GetElementFilter(self):
        return NS(PassesFilter=lambda _, identifier: identifier.Value == 102)


class View:
    IsTemplate = False
    Id = Id(90)
    UniqueId = "active-view"

    def __init__(self):
        self.ViewTemplateId = Id(-1)
        self.direct = {101, 102}
        self.categories = {-11: True, -12: False}
        self.order = [201, 202, 203]
        self.flags = {201: (True, False), 202: (True, False), 203: (False, False)}
        self.controls = set()
        self.dependents = []
        self.TemporaryViewModes = NS(IsModeAvailable=lambda mode: True, IsModeEnabled=lambda mode: False)

    def AreGraphicsOverridesAllowed(self):
        return True

    def GetPrimaryViewId(self):
        return Id(-1)

    def GetDependentViewIds(self):
        return self.dependents

    def GetPlacementOnSheetStatus(self):
        return "NotPlaced"

    def GetTemplateParameterIds(self):
        return [Id(item) for item in self.controls]

    def GetNonControlledTemplateParameterIds(self):
        return []

    def GetCategoryHidden(self, identifier):
        return self.categories[identifier.Value]

    def CanCategoryBeHidden(self, _):
        return True

    def GetOrderedFilters(self):
        return [Id(item) for item in self.order]

    def GetIsFilterEnabled(self, identifier):
        return self.flags[identifier.Value][0]

    def GetFilterVisibility(self, identifier):
        return self.flags[identifier.Value][1]

    def UnhideElements(self, identifiers):
        assert all(isinstance(item, Id) for item in identifiers)
        self.direct.difference_update(item.Value for item in identifiers)

    def SetCategoryHidden(self, identifier, hidden):
        self.categories[identifier.Value] = hidden

    def SetFilterVisibility(self, identifier, visible):
        assert identifier.Value in self.order
        self.flags[identifier.Value] = (self.flags[identifier.Value][0], visible)


class Collector:
    def __init__(self, doc):
        self.doc = doc

    def OfCategoryId(self, identifier):
        self.category = identifier.Value
        return self

    def WhereElementIsNotElementType(self):
        return [row for row in self.doc.elements.values()
                if isinstance(row, Element) and row.Category.Id.Value == self.category]


class Transaction:
    def __init__(self, doc, _):
        self.doc = doc
        self.state = "New"
        self.original = {name: copy.deepcopy(getattr(doc.ActiveView, name))
                         for name in ("direct", "categories", "flags", "order")}
        doc.transactions.append(self)

    def Start(self):
        self.state = "Started"
        return self.state

    def GetStatus(self):
        return self.state

    def Commit(self):
        self.state = "Committed"
        return self.state

    def RollBack(self):
        for name, value in self.original.items():
            setattr(self.doc.ActiveView, name, value)
        self.state = "RolledBack"
        return self.state


class TypedList(list):
    def Add(self, element):
        self.append(element)


@pytest.fixture
def host(monkeypatch):
    categories = {value: NS(Id=Id(value), CategoryType="Model", Parent=None, SubCategories=[])
                  for value in (-11, -12)}
    active = View()
    elements = {101: Element(101, categories[-11]), 102: Element(102, categories[-11]),
                103: Element(103, categories[-12]), 201: SelectionFilter(201, [101]),
                202: ParameterFilter(), 203: SelectionFilter(203, [103])}
    doc = NS(IsFamilyDocument=False, IsReadOnly=False, IsWorkshared=False, PathName="fixture.rvt",
             ProjectInformation=NS(UniqueId="project"), ActiveView=active, elements=elements,
             transactions=[], Regenerate=lambda: None)
    doc.GetElement = lambda identifier: elements.get(identifier.Value)
    db = NS(ElementId=Id, FilteredElementCollector=Collector,
            Category=NS(GetCategory=lambda _, identifier: categories.get(identifier.Value)),
            SelectionFilterElement=SelectionFilter, ParameterFilterElement=ParameterFilter,
            ElementType=type("Type", (), {}), ImportInstance=type("Import", (), {}),
            RevitLinkInstance=type("Link", (), {}),
            BuiltInParameter=NS(VIS_GRAPHICS_MODEL=-21, VIS_GRAPHICS_ANNOTATION=-22, VIS_GRAPHICS_FILTERS=-23),
            TemporaryViewMode=NS(RevealHiddenElements=1, TemporaryHideIsolate=2, TemporaryViewProperties=3),
            Transaction=Transaction, TransactionStatus=NS(Started="Started", Committed="Committed",
                                                          RolledBack="RolledBack"))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List={Id: TypedList}))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    monkeypatch.setitem(sys.modules, "pyrevit", NS(api=NS(DB=db)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    ui = NS(Selection=NS(GetElementIds=lambda: [Id(103)]))

    def run(inputs):
        return runpy.run_path(str(SCRIPTS / "hidden_elements.py"), init_globals={
            "doc": doc, "inputs": inputs, "__revit__": NS(ActiveUIDocument=ui),
        })["result"]

    return doc, active, run


def read(run, whole=None):
    scope = {"element_ids": ["101", "102", "103"]}
    if whole:
        scope["whole"] = {"kind": whole[0], "id": whole[1]}
    result = run({"scope": scope})
    assert result["status"] == "ok", result
    return json.loads(result["snapshot_json"])


@pytest.mark.parametrize("action,target", [("element", "101"), ("category", "-11"), ("filter", "201")])
def test_each_action_changes_exactly_reviewed_flag(host, action, target):
    doc, view, run = host
    old = read(run, None if action == "element" else (action, target))
    plan = preview.compute(old, {"kind": action, "ids": [target], "allow_whole_view": action != "element"})
    result = run({"mode": "apply", "plan": plan})
    assert result["status"] == "ok", result
    assert doc.transactions[0].state == "Committed"
    preview.check_after(plan, read(run, None if action == "element" else (action, target)))
    assert 102 in view.direct
    assert view.order == [201, 202, 203]
    assert view.flags[202] == (True, False) and view.flags[203] == (False, False)


def test_diagnosis_distinguishes_template_direct_and_disabled_filter(host):
    doc, view, run = host
    template = View()
    template.IsTemplate = True
    template.UniqueId = "shared-template"
    template.controls = {-21, -23}
    view.ViewTemplateId = Id(99)
    doc.elements[99] = template
    diagnostic = preview.report(read(run))
    assert diagnostic["rows"][0]["reasons"] == [
        {"kind": "element", "origin": "view"},
        {"kind": "category", "origin": "template", "id": "-11"},
        {"kind": "filter", "origin": "template", "id": "201"},
    ]
    assert diagnostic["rows"][1]["matching_filter_order"] == ["202"]
    assert diagnostic["rows"][2]["reasons"] == []
    plan = preview.compute(read(run), {"kind": "element", "ids": ["101"]})
    assert plan["warnings"] == [{
        "row": "101", "blocking": False,
        "text": "Other configured hides remain for category -11 (template), filter 201 (template).",
    }]
    assert run({"mode": "apply", "plan": plan})["status"] == "ok"
    remaining = preview.report(read(run))["rows"][0]["reasons"]
    assert remaining == diagnostic["rows"][0]["reasons"][1:]
    with pytest.raises(ValueError, match="editable local"):
        preview.compute(read(run, ("filter", "201")),
                        {"kind": "filter", "ids": ["201"], "allow_whole_view": True})
    template.categories[-11] = False
    diagnostic = preview.report(read(run))
    assert diagnostic["rows"][0]["reasons"][0]["origin"] == "unknown"
    assert diagnostic["rows"][0]["incomplete_evidence"]


def test_enabled_unsupported_filter_is_unknown(host):
    doc, _, run = host
    doc.elements[201] = NS(UniqueId="unsupported-filter")
    diagnostic = preview.report(read(run))
    first = diagnostic["rows"][0]
    assert first["incomplete_evidence"]
    assert all(reason.get("id") != "201" for reason in first["reasons"])
    response = run({"scope": {"element_ids": ["101"], "whole": {"kind": "filter", "id": "201"}}})
    assert response["status"] == "stopped"


@pytest.mark.parametrize("change", ["element", "dependent", "filter", "temporary"])
def test_stale_state_stops_before_transaction(host, change):
    doc, view, run = host
    kind = "filter" if change == "filter" else "element"
    identifier = "201" if kind == "filter" else "101"
    old = read(run, (kind, identifier) if kind == "filter" else None)
    plan = preview.compute(old, {"kind": kind, "ids": [identifier], "allow_whole_view": kind == "filter"})
    if change == "element":
        view.direct.remove(102)
    elif change == "filter":
        view.order.remove(201)
    elif change == "dependent":
        view.dependents = [Id(91)]
    else:
        view.TemporaryViewModes.IsModeEnabled = lambda mode: mode == 1
    assert run({"mode": "apply", "plan": plan})["status"] == "stopped"
    assert doc.transactions == []


def test_unrelated_flag_change_rolls_back(host):
    doc, view, run = host
    plan = preview.compute(read(run), {"kind": "element", "ids": ["101"]})
    doc.Regenerate = lambda: view.direct.remove(102)
    assert run({"mode": "apply", "plan": plan})["status"] == "stopped"
    assert view.direct == {101, 102}
    assert doc.transactions[0].state == "RolledBack"


def test_cli_uses_actual_runtime_envelope_and_rejects_unexpected_ids(host, tmp_path):
    _, _, run = host
    read_file = tmp_path / "read.json"
    read_file.write_text(json.dumps({"result": run({"scope": {"category_ids": ["-11"]}})}))
    intent = tmp_path / "intent.json"
    intent.write_text(json.dumps({"kind": "element", "ids": ["101"]}))
    plan_file = tmp_path / "plan.json"
    cli = [sys.executable, str(SCRIPTS / "hidden_elements_preview.py")]
    subprocess.run(cli + ["compute", str(read_file), str(intent), str(plan_file)], check=True)
    plan = json.loads(plan_file.read_text())
    response = run({"mode": "apply", "plan": plan})
    changes = {"added_count": 0, "deleted_count": 0, "modified_count": 2,
               "modified": [{"id": 90}, {"id": 101}], "by_category": {"Views": 1, "Walls": 1}}
    dry = tmp_path / "dry.json"
    dry.write_text(json.dumps({"result": response, "changes": changes}))
    subprocess.run(cli + ["side-effects", str(plan_file), str(dry)], check=True)
    changes["modified"][1]["id"] = 102
    dry.write_text(json.dumps({"result": response, "changes": changes}))
    assert subprocess.run(cli + ["side-effects", str(plan_file), str(dry)]).returncode == 2
