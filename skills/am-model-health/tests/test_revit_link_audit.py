"""Run the same source entry point as pyRevit with a small, read-only DB fake."""
import json
from pathlib import Path
import runpy
from types import SimpleNamespace as NS
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "model_health.py"
VIEW_ID = 9007199254740993


def fail():
    raise RuntimeError("secret-provider-token")


class ElementId:
    def __init__(self, value):
        self.Value = value


class View:
    IsTemplate = False

    def __init__(self, identifier):
        self.Id = ElementId(identifier)
        self.hidden_categories = set()

    def GetCategoryHidden(self, category):
        return category.Value in self.hidden_categories


class Collector:
    def __init__(self, document, view=None):
        self.doc = document
        self.view = view

    def OfClass(self, cls):
        self.cls = cls
        return self

    def ToElementIds(self):
        self.doc.calls.append((self.cls, self.view.Value if self.view else None))
        if self.view:
            ids = self.doc.visibility[self.view.Value]
            if ids is None:
                return fail()
        else:
            ids = [key for key, el in self.doc.elements.items() if isinstance(el, self.cls)]
        return [ElementId(key) for key in reversed(ids)]

    @staticmethod
    def IsViewValidForElementIteration(document, identifier):
        return identifier.Value in document.visibility


class LinkType:
    IsNestedLink = False
    PathType = "Relative"

    def __init__(self, identifier, path, status="Loaded"):
        self.Id = ElementId(identifier)
        self.path = path
        self.status = status
        self.local = False
        self.resources = {}
        self.source = None

    def GetExternalFileReference(self):
        if self.path is None:
            return fail()
        model = NS(path=self.path, CloudPath=False, ServerPath=False)
        return NS(GetAbsolutePath=lambda: model, GetPath=lambda: model,
                  GetLinkedFileStatus=lambda: self.status)

    def GetExternalResourceReference(self, kind):
        return self.resources.get(kind)

    def GetConversionData(self):
        return NS(Path=self.source)

    def GetLinkedFileStatus(self):
        return self.status

    def IsFromLocalPath(self):
        return self.local

    def IsFromRevitServer(self):
        return False

    @staticmethod
    def IsLoaded(document, identifier):
        return document.elements[identifier.Value].status == "Loaded"


class LinkInstance:
    def __init__(self, identifier, type_id, label="Survey", pinned=True):
        self.Id = ElementId(identifier)
        self.type_id = type_id
        self.label = label
        self.Pinned = pinned
        self.Category = NS(Id=ElementId(55))
        self.hidden = set()

    def GetTypeId(self):
        return ElementId(self.type_id)

    def get_Parameter(self, key):
        assert key == "site"
        return NS(StorageType="String", AsString=lambda: self.label)

    def GetTotalTransform(self):
        return NS(Origin=NS(X=10, Y=20, Z=0))

    def IsHidden(self, view):
        return view.Id.Value in self.hidden


class Document:
    IsFamilyDocument = False

    def __init__(self, elements, visibility):
        self.elements = {item.Id.Value: item for item in elements}
        self.visibility = visibility
        self.ActiveView = self.elements.get(VIEW_ID)
        self.calls = []

    def GetElement(self, identifier):
        return self.elements.get(identifier.Value)


@pytest.fixture
def query(monkeypatch):
    db = NS(View=View, RevitLinkType=LinkType, RevitLinkInstance=LinkInstance,
            FilteredElementCollector=Collector, ElementId=ElementId,
            ModelPathUtils=NS(ConvertModelPathToUserVisiblePath=lambda model: model.path),
            BuiltInParameter=NS(GEO_LOCATION="site"), StorageType=NS(String="String"),
            ExternalResourceTypes=NS(BuiltInExternalResourceTypes=NS(IFCLink="ifc", RevitLink="rvt")))
    system = NS(Int64=int, Int32=int, Diagnostics=NS(
        Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    monkeypatch.setitem(sys.modules, "pyrevit", NS(api=NS(DB=db)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", system)

    def execute(document, inputs=None, year="2026"):
        return runpy.run_path(str(SCRIPT), init_globals={
            "doc": document, "app": NS(VersionNumber=year),
            "inputs": {"revit_link_audit": True} if inputs is None else inputs,
        })["result"]

    return execute, system


def test_link_audit_uses_runtime_entry_point_and_separates_source_and_cache(query, capsys):
    execute, _ = query
    personal = LinkType(1, r"C:\Users\private-owner\Documents\secret-model.rvt")
    ifc = LinkType(2, r"C:\Cache\secret-model.ifc.rvt")
    ifc.local = True
    ifc.resources["ifc"] = NS(InSessionPath=r"\\server\share\secret-model.ifc")
    ifc.source = r"\\server\share\secret-model.ifc"
    missing = LinkType(3, r"\\server\share\unavailable.rvt", "NotFound")
    provider = LinkType(4, None)
    provider.resources["rvt"] = NS(InSessionPath="Autodesk Docs://secret-provider-token/Name.rvt")
    provider.IsNestedLink = True
    cache_only = LinkType(5, r"C:\Cache\another.ifc.rvt")
    first = LinkInstance(11, 1)
    first.hidden.add(42)
    second = LinkInstance(12, 2, label=r"C:\Users\private-owner", pinned=False)
    third = LinkInstance(13, 3)
    view_a, view_b = View(VIEW_ID), View(42)
    doc = Document([personal, ifc, missing, provider, cache_only, first, second, third, view_a, view_b],
                   {VIEW_ID: [11, 12, 13], 42: [11]})
    result = execute(doc, {"revit_link_audit": True, "link_view_ids": [str(VIEW_ID), 42]})
    report = result["revit_links"]
    one, two, three, four, five = report["types"]
    assert one["source_path_kind"] == one["path_risk"] == "personal"
    assert one["saved_model_path_type"] == "Relative"
    assert (two["format"], two["ifc_evidence"], two["model_path_kind"], two["source_path_kind"],
            two["path_risk"]) == ("IFC", "resource", "local", "network", None)
    assert (three["status"], three["loaded"]) == ("NotFound", False)
    assert (four["status"], four["source_path_kind"], four["nested"], four["placed"]) == (
        "Loaded", "cloud", True, False)
    assert (five["format"], five["ifc_evidence"], five["source_path_kind"]) == (None, "cache_name", None)
    assert report["instances"][0]["shared_site"] == {"state": "reported", "label": "Survey"}
    assert report["instances"][0]["origin_ft"] == [10, 20, 0]
    assert report["instances"][1]["shared_site"] == {"state": "withheld", "label": None}
    assert report["instances"][1]["pinned"] is False
    assert report["views"][0]["id"] == str(VIEW_ID)
    assert [[row["state"] for row in view["observations"]] for view in report["views"]] == [
        ["potentially_visible", "potentially_visible", "unloaded"],
        ["explicitly_hidden", "not_returned", "unloaded"],
    ]
    assert len(doc.calls) == 4
    output = json.dumps(result) + capsys.readouterr().out
    for secret in ("private-owner", "secret-model", "secret-provider-token", "unavailable.rvt"):
        assert secret not in output


def test_long_windows_paths_and_conflicting_ifc_source_risk(query, capsys):
    execute, _ = query
    profile = LinkType(1, r"\\?\C:\Users\private-long-owner\OneDrive\private-long-model.rvt")
    network = LinkType(2, r"\\?\UNC\private-long-server\share\private-long-model.rvt")
    device = LinkType(3, r"\\.\private-device\private-long-model.rvt")
    relative_components = LinkType(4, r"C:\Projects\..\Users\private-long-owner\Desktop\model.rvt")
    ifc = LinkType(5, r"C:\Cache\private-long-model.ifc.rvt")
    ifc.resources["ifc"] = NS(InSessionPath=r"\\private-long-server\share\private-long-model.ifc")
    ifc.source = r"\\?\C:\Users\private-long-owner\Documents\private-long-model.ifc"
    result = execute(Document([profile, network, device, relative_components, ifc], {}))
    rows = result["revit_links"]["types"]
    assert [(row["source_path_kind"], row["path_risk"]) for row in rows] == [
        ("personal", "personal"), ("network", None), (None, None), ("personal", "personal"), (None, "personal"),
    ]
    assert rows[-1]["source_path_kind_candidates"] == ["network", "personal"]
    output = json.dumps(result) + capsys.readouterr().out
    assert all(value not in output for value in (
        "private-long-owner", "private-long-model", "private-long-server", "private-device"))


def test_rvt_audit_keeps_personal_saved_path_when_resolved_path_is_shared(query, capsys):
    execute, _ = query
    shared = NS(path=r"\\private-server\share\private-model.rvt", CloudPath=False, ServerPath=False)
    personal = NS(path=r"C:\Users\private-owner\Documents\private-model.rvt", CloudPath=False, ServerPath=False)
    conflicting = LinkType(1, None)
    conflicting.PathType = "Absolute"
    conflicting.GetExternalFileReference = lambda: NS(GetAbsolutePath=lambda: shared, GetPath=lambda: personal)
    control = LinkType(2, None)
    control.PathType = "Relative"
    control.GetExternalFileReference = lambda: NS(
        GetAbsolutePath=lambda: personal, GetPath=lambda: NS(path=r"..\links\private-model.rvt"))
    result = execute(Document([conflicting, control], {}))
    first, second = result["revit_links"]["types"]
    assert (first["model_path_kind"], first["source_path_kind"], first["path_risk"]) == ("network", None, "personal")
    assert first["source_path_kind_candidates"] == ["network", "personal"]
    assert (second["saved_model_path_type"], second["source_path_kind"], second["path_risk"]) == (
        "Relative", "personal", "personal")
    output = json.dumps(result) + capsys.readouterr().out
    assert all(value not in output for value in ("private-owner", "private-model", "private-server"))


@pytest.mark.parametrize(("workshared", "expected"), [(False, "personal"), (True, None)])
def test_relative_path_uses_standalone_host_only_when_absolute_path_is_unavailable(query, workshared, expected):
    execute, _ = query
    link = LinkType(1, None)
    link.GetExternalFileReference = lambda: NS(
        GetAbsolutePath=fail, GetPath=lambda: NS(path=r"..\links\fixture.rvt"))
    document = Document([link], {})
    document.PathName = r"C:\Users\private-owner\Office\Host.rvt"
    document.IsWorkshared, document.IsModelInCloud = workshared, False
    row, = execute(document)["revit_links"]["types"]
    assert row["saved_model_path_type"] == "Relative"
    assert row["source_path_kind"] == row["path_risk"] == expected


def test_unreadable_absolute_path_preserves_saved_evidence_without_inventing_locality(query, capsys):
    execute, _ = query
    opaque = NS(CloudPath=False, ServerPath=False)
    personal = LinkType(1, None)
    personal.local = True
    personal.GetExternalFileReference = lambda: NS(
        GetAbsolutePath=lambda: opaque,
        GetPath=lambda: NS(path=r"C:\Users\private-profile\Documents\private-saved.rvt"))
    relative = LinkType(2, None)
    relative.local = True
    relative.GetExternalFileReference = lambda: NS(
        GetAbsolutePath=fail, GetPath=lambda: NS(path=r"..\links\private-relative.rvt"))
    cloud = LinkType(3, None)
    cloud.GetExternalFileReference = lambda: NS(
        GetAbsolutePath=lambda: NS(CloudPath=True, ServerPath=False),
        GetPath=lambda: NS(path=r"C:\Users\private-profile\Documents\private-cache.rvt"))
    result = execute(Document([personal, relative, cloud], {}))
    one, two, three = result["revit_links"]["types"]
    assert (one["format"], one["source_path_kind"], one["path_risk"]) == ("RVT", "personal", "personal")
    assert (two["format"], two["saved_model_path_type"], two["source_path_kind"], two["path_risk"]) == (
        "RVT", "Relative", None, None)
    assert (three["source_path_kind"], three["path_risk"]) == ("cloud", None)
    output = json.dumps(result) + capsys.readouterr().out
    assert all(value not in output for value in ("private-profile", "private-saved", "private-relative", "private-cache"))


def test_unavailable_collector_cannot_claim_absence_and_templates_are_invalid(query):
    execute, _ = query
    view = View(VIEW_ID)
    template = View(42)
    template.IsTemplate = True
    doc = Document([view, template, LinkType(1, None), LinkInstance(11, 1)],
                   {VIEW_ID: None, 42: []})
    views = execute(doc, {"revit_link_audit": True, "link_view_ids": [str(VIEW_ID), 42]})["revit_links"]["views"]
    assert views[0]["reason"] == "collector_unavailable"
    assert views[0]["observations"] == [
        {"instance_id": "11", "state": "unknown", "collector_member": None,
         "instance_hidden": False, "category_hidden": False}
    ]
    assert views[1]["reason"] == "invalid_view" and views[1]["omitted"] == 1
    assert doc.calls[-1] == (LinkInstance, VIEW_ID)


def test_category_hide_and_partial_link_evidence_remain_distinct(query):
    execute, _ = query
    link_type = LinkType(1, None)
    link_type.GetExternalFileReference = lambda: NS(
        GetAbsolutePath=fail,
        GetPath=lambda: NS(path="opaque", CloudPath=True, ServerPath=False),
    )
    instance = LinkInstance(11, 1)
    first, second = View(VIEW_ID), View(42)
    first.hidden_categories.add(55)
    second.GetCategoryHidden = lambda category: fail()
    doc = Document([link_type, instance, first, second], {VIEW_ID: [11], 42: [11]})
    report = execute(doc, {"revit_link_audit": True, "link_view_ids": [str(VIEW_ID), 42]})["revit_links"]
    assert report["types"][0]["model_path_kind"] == "cloud"
    assert [row["observations"][0]["state"] for row in report["views"]] == [
        "explicitly_hidden", "unknown",
    ]


@pytest.mark.parametrize("ids", [[True], [1, "1"], [0], [str(2**63)], [1.0], list(range(1, 12)), []])
def test_invalid_view_inputs_stop_before_collecting(query, ids):
    execute, _ = query
    doc = Document([], {})
    assert execute(doc, {"revit_link_audit": True, "link_view_ids": ids})["error"] == "invalid_link_view_ids"
    assert doc.calls == []


@pytest.mark.parametrize("inputs", [{}, {"revit_link_audit": False}, {"revit_link_audit": "true"}])
def test_default_still_dispatches_general_health_query(query, inputs):
    execute, _ = query
    doc = Document([], {})

    def general_health_called():
        raise RuntimeError("general audit started")

    doc.GetWarnings = general_health_called
    with pytest.raises(RuntimeError, match="general audit started"):
        execute(doc, inputs)
    assert doc.calls == []


def test_caps_observations_and_marks_incomplete_inventory(query):
    execute, _ = query
    types = [LinkType(1, None), LinkType(2, None)]
    instances = [LinkInstance(i, 1) for i in range(100, 301)]
    views = [View(i) for i in range(20, 26)]
    doc = Document(types + instances + views, {v.Id.Value: [] for v in views})
    report = execute(doc, {"revit_link_audit": True, "link_view_ids": list(range(20, 26))})["revit_links"]
    assert report["instance_coverage"] == {"total": 201, "emitted": 200, "omitted": 1,
                                            "reason": "inventory_limit"}
    assert report["instances"][0]["id"] == "100"
    assert report["types"][1]["placed"] is None
    assert [len(view["observations"]) for view in report["views"]] == [200, 200, 200, 200, 200, 0]
    assert report["views"][-1]["reason"] == "observation_limit"
    assert len(doc.calls) == 7


def test_elapsed_time_during_inventory_preserves_unknown_coverage(query):
    execute, system = query
    clock = NS(ElapsedMilliseconds=0)
    system.Diagnostics.Stopwatch.StartNew = lambda: clock
    link_type = LinkType(1, None)
    view = View(VIEW_ID)
    doc = Document([link_type, view], {VIEW_ID: []})
    get_element = doc.GetElement

    def consume_budget(identifier):
        clock.ElapsedMilliseconds = 10000
        return get_element(identifier)

    doc.GetElement = consume_budget
    report = execute(doc)["revit_links"]
    assert report["view_scope"] == "active"
    assert report["instance_coverage"]["total"] is None
    assert report["instance_coverage"]["reason"] == report["views"][0]["reason"] == "time_limit"
    assert report["types"][0]["placed"] is None
    assert doc.calls == [(LinkType, None)]
