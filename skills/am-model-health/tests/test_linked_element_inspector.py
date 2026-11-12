import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_linked_element.py"
LARGE = 9007199254740993


class Id:
    def __init__(self, value):
        self.Value = int(value)


class References(list):
    Add = list.append

    @property
    def Count(self):
        return len(self)


class List:
    @classmethod
    def __class_getitem__(cls, _):
        return References


class Reference:
    def __init__(self, element=None, link=-1, linked=-1, stable=None):
        self.element = element
        self.ElementId, self.LinkedElementId = Id(link), Id(linked)
        self.stable = stable or f"{link}:{linked}"

    def CreateLinkReference(self, link):
        return Reference(link=link.Id.Value, linked=self.element.Id.Value)

    def ConvertToStableRepresentation(self, _doc):
        return self.stable


class LinkType:
    IsNestedLink = False


class Link:
    def __init__(self, eid, document):
        self.Id, self.UniqueId, self.Name = Id(eid), f"placement-{eid}", f"Placement {eid}"
        self.document = document
        self.origin = NS(X=float(eid), Y=0.0, Z=0.0)

    def GetTransform(self):
        return NS(Origin=self.origin, BasisX=NS(X=1.0, Y=0.0, Z=0.0),
                  BasisY=NS(X=0.0, Y=1.0, Z=0.0), BasisZ=NS(X=0.0, Y=0.0, Z=1.0))

    def GetTypeId(self):
        return Id(40)

    def GetLinkDocument(self):
        return self.document


class Document:
    IsFamilyDocument = False
    IsModelInCloud = False

    def __init__(self, name, items):
        self.Title, self.PathName = name, name + ".rvt"
        self.ProjectInformation = NS(UniqueId=name + "-project")
        self.items = items

    def GetElement(self, eid):
        return self.items.get(eid.Value)

    def Equals(self, other):
        return self is other


class Collector:
    def __init__(self, document):
        self.document = document

    def OfCategoryId(self, category):
        self.category = category
        return self

    def WhereElementIsNotElementType(self):
        return (item for item in self.document.items.values() if item.Category.Id.Value == self.category.Value)


def parameter(eid, name, storage, value):
    return NS(Id=Id(eid), Definition=NS(Name=name), StorageType=storage, HasValue=value is not None,
              AsString=lambda: value, AsInteger=lambda: value, AsDouble=lambda: value,
              AsElementId=lambda: Id(value), AsValueString=lambda: None)


def element(eid, name, parameters=()):
    return NS(Id=Id(eid), Name=name, UniqueId=f"element-{eid}",
              Category=NS(Id=Id(-2000014), Name="Doors"),
              GetOrderedParameters=lambda: References(parameters),
              GetParameters=lambda label: References(row for row in parameters if row.Definition.Name == label))


@pytest.fixture
def fixture(monkeypatch):
    db = NS(ElementId=Id, ElementType=type("ElementType", (), {}), Reference=Reference,
            RevitLinkInstance=Link, RevitLinkType=LinkType, FilteredElementCollector=Collector,
            Element=NS(Name=NS(GetValue=lambda item: item.Name)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=List))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    linked_element = element(LARGE, "Door café", [
        parameter(-123, "Mark", "String", "D-01"),
        parameter(12, "Mark", "String", "Second field"),
        parameter(13, "Level", "ElementId", -1),
        parameter(14, "Length", "Double", 1.25),
    ])
    linked = Document("Link café", {LARGE: linked_element})
    host = Document("Host A", {5: Link(5, linked), 6: Link(6, linked), 40: LinkType(),
                              LARGE: element(LARGE, "Different host element")})
    current = [Reference(link=5, linked=LARGE)]
    calls = []

    def set_refs(items):
        calls.append(list(items))
        current[:] = items

    selection = NS(GetReferences=lambda: References(current), SetReferences=set_refs)
    ui = NS(Document=host, Selection=selection, ActiveView=NS(Id=Id(77), UniqueId="view-one", IsTemplate=False))

    def run(**inputs):
        result = runpy.run_path(str(SCRIPT), init_globals={"doc": host, "uidoc": ui, "inputs": inputs})["result"]
        return json.loads(result["payload"]) if "payload" in result else result

    return NS(run=run, linked=linked, host=host, ui=ui, current=current, calls=calls)


def test_picked_id_parameters_and_exact_repeated_link_selection(fixture):
    picked = fixture.run(parameter_names=["Mark", "Level", "Length", "Missing"])
    assert picked["link_instance_id"] == "5"
    assert picked["linked_element_id"] == str(LARGE)
    assert picked["element_name"] == "Door café"
    assert picked["linked_document_title"] == "Link café"
    assert picked["missing_parameter_names"] == ["Missing"]
    assert [row["id"] for row in picked["parameters"]] == ["-123", "12", "13", "14"]
    assert [row["value"] for row in picked["parameters"]] == ["D-01", "Second field", "-1", 1.25]
    assert not fixture.calls

    second = fixture.run(link_instance_id="6", category_id="-2000014", name="Door café")
    target = second["selection_target"]
    assert target["linked_element_uid"] == f"element-{LARGE}"
    assert target["link_instance_uid"] == "placement-6"
    assert "PathName" not in json.dumps(second)
    selected = fixture.run(mode="select", target=target)
    assert selected["status"] == "selected"
    assert fixture.run(mode="selection")["references"] == [{"link_instance_id": "6", "linked_element_id": str(LARGE)}]
    assert len(fixture.calls) == 1

    fixture.ui.ActiveView.UniqueId = "other-view"
    assert fixture.run(mode="select", target=target)["status"] == "stopped"
    assert len(fixture.calls) == 1


def test_moved_link_requires_reinspection_even_when_element_ids_are_unchanged(fixture):
    target = fixture.run(link_instance_id="6", linked_element_id=str(LARGE))["selection_target"]
    fixture.host.items[5].origin.X += 1
    assert fixture.run(mode="select", target=target)["status"] == "selected"
    fixture.host.items[6].origin.X += 1
    assert fixture.run(mode="select", target=target)["status"] == "stopped"
    assert len(fixture.calls) == 1
    fresh = fixture.run(link_instance_id="6", linked_element_id=str(LARGE))["selection_target"]
    assert fixture.run(mode="select", target=fresh)["status"] == "selected"
    fixture.host.items[6].origin.X = float("nan")
    inspected = fixture.run(link_instance_id="6", linked_element_id=str(LARGE))
    assert inspected["linked_element_id"] == str(LARGE) and inspected["selection_target"] is None


def test_name_ambiguity_unresolved_and_cloud_identity(fixture):
    fixture.linked.items[88] = element(88, "Door café")
    ambiguous = fixture.run(link_instance_id="5", category_id="-2000014", name="Door café")
    assert ambiguous == {"status": "ambiguous", "matches": 2, "candidate_ids": ["88", str(LARGE)],
                         "candidates_omitted": 0, "link_instance_id": "5"}
    fixture.linked.PathName = ""
    inspected = fixture.run(link_instance_id="5", linked_element_id=str(LARGE))
    assert inspected["status"] == "inspected" and inspected["selection_target"] is None
    fixture.linked.IsModelInCloud = True
    fixture.linked.GetCloudModelPath = lambda: NS(Region="US", GetProjectGUID=lambda: "cloud-project")
    fixture.linked.CloudModelGUID = "cloud-model"
    inspected = fixture.run(link_instance_id="5", linked_element_id=str(LARGE))
    assert inspected["selection_target"]["linked_document"]["kind"] == "cloud"
    fixture.host.items[40].IsNestedLink = True
    assert fixture.run(link_instance_id="5", linked_element_id=str(LARGE))["status"] == "stopped"


def test_restores_exact_subelements_if_setter_throws_after_mutation(fixture):
    target = fixture.run(link_instance_id="6", linked_element_id=str(LARGE))["selection_target"]
    prior = [Reference(link=5, linked=LARGE, stable="face-a"), Reference(link=5, linked=LARGE, stable="face-b")]
    fixture.current[:] = prior

    def failing_setter(items):
        fixture.calls.append(list(items))
        fixture.current[:] = items
        if len(fixture.calls) == 1:
            raise RuntimeError("selection event error")

    fixture.ui.Selection.SetReferences = failing_setter
    failure = fixture.run(mode="select", target=target)
    assert failure == {"status": "selection_failed", "reason": "selection_operation_failed",
                       "error_type": "RuntimeError", "previous_selection": "restored"}
    assert fixture.current == prior
    assert fixture.calls[1] == prior


def test_same_element_ids_on_a_different_subelement_restore_selection(fixture):
    target = fixture.run(link_instance_id="6", linked_element_id=str(LARGE))["selection_target"]
    prior = list(fixture.current)
    substituted = Reference(link=6, linked=LARGE, stable="different-face")

    def substitute_once(items):
        fixture.calls.append(list(items))
        fixture.current[:] = [substituted] if len(fixture.calls) == 1 else items

    fixture.ui.Selection.SetReferences = substitute_once
    failure = fixture.run(mode="select", target=target)
    assert failure == {"status": "selection_failed", "reason": "reference_readback_mismatch",
                       "previous_selection": "restored"}
    assert fixture.current == prior
    assert fixture.calls[1] == prior


def test_bounds_response_and_withholds_target_on_incomplete_name_search(fixture, monkeypatch):
    item = fixture.linked.items[LARGE]
    many = References(parameter(100 + i, f"Label {i}", "String", "\U0001f600" * 2100) for i in range(40))
    item.GetOrderedParameters = lambda: many
    reported = fixture.run(link_instance_id="5", linked_element_id=str(LARGE))
    assert reported["status"] == "inspected" and reported["parameters_limited"]
    assert 0 < len(reported["parameters"]) < 40
    assert reported["parameters"][0]["value_truncated"] is True
    assert len(reported["parameters"][0]["value"]) == 2000

    clock = NS(StartNew=lambda: NS(ElapsedMilliseconds=8000))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=clock))
    partial = fixture.run(link_instance_id="5", category_id="-2000014", name="Door café")
    assert partial["status"] == "incomplete"
    assert "selection_target" not in partial
    assert not fixture.calls
