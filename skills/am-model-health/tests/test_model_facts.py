"""Exercise the model-facts entry point with the Revit API reads it uses."""

import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "read_model_facts.py"


class LinkType:
    IsNestedLink = False

    def __init__(self, identifier, status):
        self.Id = NS(Value=identifier)
        self.Name = "Link " + str(identifier)
        self.status = status

    def GetLinkedFileStatus(self):
        return self.status

    def GetExternalFileReference(self):
        return NS(GetPath=lambda: r"links\structure.rvt",
                  GetAbsolutePath=lambda: r"Z:\models\links\structure.rvt")

    def GetExternalResourceReference(self, kind):
        return NS(InSessionPath="Autodesk Docs://Project/Structure.rvt")


class LinkInstance:
    def GetTypeId(self):
        return NS(Value=9007199254740993)

    def GetLinkDocument(self):
        return NS(Title="Structure – 配管")


@pytest.fixture
def project(monkeypatch):
    doc = NS(Title=r"Modèle \u4e2d", PathName=r"C:\Working\Accès.rvt",
             ProjectInformation=NS(UniqueId="project-uid"), IsFamilyDocument=False,
             IsWorkshared=False, IsDetached=False, IsModelInCloud=False,
             worksets=[NS(Kind="UserWorkset"), NS(Kind="ViewWorkset")], links=[], instances=[])
    db = NS(
        ModelPathUtils=NS(ConvertModelPathToUserVisiblePath=lambda path: getattr(path, "path", path)),
        FilteredWorksetCollector=lambda target: iter(target.worksets),
        WorksetKind=NS(UserWorkset="UserWorkset"),
        RevitLinkType=LinkType,
        RevitLinkInstance=LinkInstance,
        FilteredElementCollector=lambda target: NS(
            OfClass=lambda cls: iter(target.links if cls is LinkType else target.instances)),
        ExternalResourceTypes=NS(BuiltInExternalResourceTypes=NS(RevitLink="rvt")),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    return doc


def query(doc, **inputs):
    expected = {"expected_title": doc.Title, "expected_path": doc.PathName}
    expected.update(inputs)
    output = runpy.run_path(str(SCRIPT), init_globals={
        "doc": doc, "app": NS(VersionNumber="2026", VersionBuild="20260215_1515(x64)"),
        "inputs": expected,
    })["result"]
    assert output.isascii()
    return json.loads(output)


def test_working_and_central_paths_remain_distinct_and_links_are_complete(project):
    project.IsWorkshared = True
    central = NS(path=r"\\server\Revit\Central.rvt", ServerPath=False)
    project.GetWorksharingCentralModelPath = lambda: central
    nested = LinkType(3, "Unloaded")
    nested.IsNestedLink = True
    project.links = [LinkType(9007199254740993, "Loaded"), nested, LinkType(2, "Unloaded")]
    project.instances = [LinkInstance(), LinkInstance()]

    output = query(project)
    assert output["status"] == "complete"
    assert output["model"] == {
        "title": r"Modèle \u4e2d", "file_name": "Accès.rvt", "current_path": r"C:\Working\Accès.rvt",
        "location": "drive_path", "workshared": True, "detached": False, "is_cloud": False,
        "central_path": r"\\server\Revit\Central.rvt", "central_file_name": "Central.rvt",
        "central_state": "available", "cloud_path": None, "cloud_file_name": None,
        "revit_year": "2026", "revit_build": "20260215_1515(x64)",
    }
    assert output["worksets"] == {"total": 2, "user": 1}
    assert output["links"] == [
        {"id": "2", "type_name": "Link 2", "status": "Unloaded",
         "stored_path": r"links\structure.rvt", "resolved_path": r"Z:\models\links\structure.rvt",
         "provider_session_path": "Autodesk Docs://Project/Structure.rvt",
         "loaded_document_titles": [], "instance_count": 0},
        {"id": "9007199254740993", "type_name": "Link 9007199254740993", "status": "Loaded",
         "stored_path": r"links\structure.rvt", "resolved_path": r"Z:\models\links\structure.rvt",
         "provider_session_path": "Autodesk Docs://Project/Structure.rvt",
         "loaded_document_titles": ["Structure – 配管"], "instance_count": 2},
    ]


@pytest.mark.parametrize("kind", ["standalone", "detached", "cloud", "revit_server", "missing_central"])
def test_model_location_and_missing_fields(project, kind):
    if kind != "standalone":
        project.IsWorkshared = True
    if kind == "detached":
        project.IsDetached = True
        project.PathName = ""
    if kind == "cloud":
        project.IsModelInCloud = True
        project.PathName = ""
        project.GetCloudModelPath = lambda: "Autodesk Docs://Project/Cloud.rvt"
        project.GetWorksharingCentralModelPath = lambda: NS(
            path=project.GetCloudModelPath(), ServerPath=False)
    if kind == "revit_server":
        project.GetWorksharingCentralModelPath = lambda: NS(
            path="RSN://Server/Model.rvt", ServerPath=True)
    output = query(project, expected_project_uid="project-uid")
    model = output["model"]
    expected_location = {"standalone": "drive_path", "missing_central": "drive_path"}.get(kind, kind)
    assert model["location"] == expected_location
    if kind == "missing_central":
        assert output["status"] == "incomplete"
        assert output["issues"] == ["central_path_unavailable"]
        assert model["central_path"] is None
    else:
        assert output["status"] == "complete"
    if kind == "cloud":
        assert model["file_name"] is None
        assert model["cloud_file_name"] == "Cloud.rvt"
    if kind in ("detached", "standalone"):
        assert model["central_state"] == "not_applicable"


def test_wrong_identity_rejects_and_empty_path_requires_verified_uid(project):
    assert query(project, expected_path="wrong") == {
        "status": "rejected", "error": "document_identity_mismatch",
    }
    project.PathName = ""
    assert query(project, identify=True) == {
        "status": "identified", "title": project.Title, "path": "", "project_uid": "project-uid",
    }
    assert query(project)["error"] == "project_uid_required_for_empty_path"
    assert query(project, expected_project_uid="other")["error"] == "document_identity_mismatch"
    assert query(project, expected_project_uid="project-uid")["model"]["location"] == "unsaved"


def test_caps_discard_incomplete_collections(project):
    project.worksets *= 501
    project.links = [LinkType(number, "Unloaded") for number in range(501)]
    output = query(project)
    assert output["status"] == "incomplete"
    assert output["worksets"] is None
    assert output["links"] is None
    assert output["issues"] == ["link_types_limit", "worksets_limit"]


def test_output_budget_refuses_excessive_link_paths(project):
    long_path = "中" * 4096
    project.links = [LinkType(number, "Unloaded") for number in range(100)]
    for link in project.links:
        link.GetExternalResourceReference = lambda kind: NS(InSessionPath=long_path)
    output = query(project)
    assert output["status"] == "incomplete"
    assert output["links"] is None
    assert output["issues"] == ["output_limit"]


def test_revit_server_central_name_is_available_without_a_working_path(project):
    project.PathName = ""
    project.IsWorkshared = True
    project.GetWorksharingCentralModelPath = lambda: NS(
        path="RSN://Server/Project/Central.rvt", ServerPath=True)
    output = query(project, expected_project_uid="project-uid")
    assert output["model"]["file_name"] is None
    assert output["model"]["central_file_name"] == "Central.rvt"
    assert output["model"]["location"] == "revit_server"
    assert output["issues"] == []
    assert output["status"] == "complete"
