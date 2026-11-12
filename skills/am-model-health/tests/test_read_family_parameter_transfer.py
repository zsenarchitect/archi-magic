from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_library_discovery import manifest_digest
from read_family_library import WorkerUnresolved
from read_family_parameter_transfer import read_result, transfer_state


def key(value):
    return SimpleNamespace(Value=value)


@pytest.fixture
def case():
    class Definition:
        Name = "Width"
        BuiltInParameter = "INVALID"

        def GetDataType(self):
            return SimpleNamespace(TypeId="length")

        def GetGroupTypeId(self):
            return SimpleNamespace(TypeId="geometry")

    class Family:
        Id = key(20)
        UniqueId = "family-20"
        FamilyCategory = SimpleNamespace(Id=key(-1000))
        Name = "Fixture"
        IsEditable = True
        IsInPlace = False

    class Dimension:
        Id = key(11)
        UniqueId = "dimension-11"

    parameter = SimpleNamespace(Id=key(42), Definition=Definition(), IsShared=False, StorageType="Double",
                                IsReadOnly=False, IsDeterminedByFormula=True, IsReporting=False, IsInstance=False,
                                Formula="Base Width * 2", CanAssignFormula=True)
    family_type = SimpleNamespace(Name="Type A", HasValue=lambda field: True, AsDouble=lambda field: 4.0,
                                  AsString=lambda field: None, AsInteger=lambda field: None, AsElementId=lambda field: None)
    app = SimpleNamespace(VersionNumber="2026", VersionBuild="fixture")
    model = SimpleNamespace(IsFamilyDocument=True, IsModified=False, IsModifiable=False, IsWorkshared=False,
                            IsModelInCloud=False, Application=app, OwnerFamily=Family(),
                            FamilyManager=SimpleNamespace(Parameters=[parameter], Types=[family_type], CurrentType=family_type))
    model.Equals = lambda value: value is model
    nested = SimpleNamespace(Id=key(12), UniqueId="nested-12", Document=model)
    parameter.AssociatedParameters = [SimpleNamespace(Id=key(61), Definition=Definition(),
                                                       IsShared=False, Element=nested)]
    model.GetElement = lambda value: nested if value.Value == 12 else None
    dimension = Dimension()
    dimension.FamilyLabel = parameter
    dimension.References = [SimpleNamespace(ElementId=key(12), ConvertToStableRepresentation=lambda doc: "fixture:ref")]
    db = SimpleNamespace(InternalDefinition=Definition, BuiltInParameter=SimpleNamespace(INVALID="INVALID"),
                         Family=Family, Dimension=Dimension, ElementId=int,
                         SpecTypeId=SimpleNamespace(Boolean=SimpleNamespace(YesNo=SimpleNamespace(TypeId="yesno"))),
                         FilteredElementCollector=lambda doc: SimpleNamespace(OfClass=lambda cls: [dimension]))
    return app, model, db, dimension, parameter


def test_snapshot_preserves_formula_values_labels_and_associations(case):
    _, model, db, _, _ = case
    state = transfer_state(model, db)
    assert state["coverage"] == {"complete": True, "issues": []}
    assert state["types"] == {"Type A": {"42": {"state": "value", "value": 4.0}}}
    definition = state["definitions"]["42"]
    assert (definition["formula"], definition["group"], definition["can_assign_formula"]) == (
        "Base Width * 2", "geometry", True,
    )
    assert definition["associations"] == [{"owner_id": "12", "owner_uid": "nested-12", "parameter_id": "61",
                                          "name": "Width", "data_type": "length",
                                          "built_in": None, "shared_guid": None}]
    assert state["dimensions"] == [{"id": "11", "uid": "dimension-11", "label_id": "42", "references": [
        {"owner_id": "12", "owner_uid": "nested-12", "stable_reference": "fixture:ref"},
    ]}]


def test_unreadable_reference_or_association_is_visible(case):
    _, model, db, dimension, parameter = case
    dimension.References[0].ElementId = key(999)
    parameter.AssociatedParameters[0].Element = None
    state = transfer_state(model, db)
    assert state["coverage"] == {"complete": False, "issues": [
        {"kind": "parameter_association", "id": "42", "error": "ValueError"},
        {"kind": "dimension_reference", "id": "11", "error": "ValueError"},
    ]}


@pytest.mark.parametrize("outcome", ["unchanged", "extra_document", "changed_value"])
def test_loaded_target_uses_independent_family_and_checks_closure(case, monkeypatch, outcome):
    import read_family_parameter_transfer as transfer

    app, family_doc, db, _, _ = case
    family = db.Family()
    family.GetFamilySymbolIds = lambda: [21]
    db.FamilySymbol = db.FamilyInstance = SimpleNamespace
    db.FilteredElementCollector = lambda doc: SimpleNamespace(
        OfClass=lambda cls: [case[3]] if doc is family_doc else [instance])
    symbol = SimpleNamespace(Id=key(21), UniqueId="symbol-21", Name="Type A", Family=family)
    project = SimpleNamespace(PathName="/fixture/Project.rvt", IsFamilyDocument=False, IsReadOnly=False,
                              IsWorkshared=False, IsModelInCloud=False, IsModified=False, IsModifiable=False,
                              ProjectInformation=SimpleNamespace(UniqueId="project-uid"),
                              GetElement=lambda value: {20: family, 21: symbol}.get(value))
    symbol.Document = project
    symbol.Parameters = [SimpleNamespace(Id=key(42), Definition=case[4].Definition, Element=symbol, IsShared=False,
                                         StorageType="Double", HasValue=True, AsDouble=lambda: 5.0)]
    instance = SimpleNamespace(Id=key(22), UniqueId="instance-22", Symbol=symbol, Document=project)
    parameter = SimpleNamespace(Id=key(43), Definition=case[4].Definition, Element=instance, IsShared=False,
                                StorageType="Double", HasValue=True, AsDouble=lambda: 7.0)
    instance.Parameters = [parameter]
    project.Equals = lambda value: value is project
    app.Documents = [project]
    ui = SimpleNamespace(Application=app, ActiveUIDocument=SimpleNamespace(Document=project))
    calls = []

    def open_family(exact):
        assert exact is family
        app.Documents.append(family_doc)
        if outcome == "extra_document":
            app.Documents.append(object())
        if outcome == "changed_value":
            parameter.AsDouble = lambda: 9.0
        return family_doc

    def close(save):
        calls.append(save)
        app.Documents.remove(family_doc)
        return True

    project.EditFamily = open_family
    family_doc.Close = close
    monkeypatch.setattr(transfer, "inventory", lambda *args: {"20": ["family-20", "v1"]})
    request = {"role": "target", "family_id": "20", "family_uid": "family-20", "revit_build": "fixture"}
    if outcome == "extra_document":
        with pytest.raises(WorkerUnresolved, match="restore the exact original"):
            read_result(ui, db, project, request)
        assert len(app.Documents) == 2 and app.Documents[0] is project
    elif outcome == "changed_value":
        with pytest.raises(WorkerUnresolved, match="project changed"):
            read_result(ui, db, project, request)
        assert app.Documents == [project]
    else:
        receipt = read_result(ui, db, project, request)
        assert receipt["loaded_family"] == {"id": "20", "uid": "family-20"}
        assert receipt["project"]["project_uid"] == "project-uid"
        assert receipt["project_values"]["types"][0]["parameters"]["42"]["value"] == 5.0
        assert receipt["project_values"]["instances"][0]["parameters"]["43"]["value"] == 7.0
        assert receipt.pop("read_sha256") == manifest_digest(receipt)
        assert app.Documents == [project]
    assert calls == [False]
