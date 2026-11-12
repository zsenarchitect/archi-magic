import json
import math
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


class Id:
    def __init__(self, value):
        self.Value = value


def xyz(x, y, z):
    return NS(X=x, Y=y, Z=z)


IDENTITY = NS(OfPoint=lambda point: point)
COS = math.sqrt(0.5)
ROTATED = NS(OfPoint=lambda point: xyz(10 + COS * (point.X - point.Y), COS * (point.X + point.Y), point.Z))
MIRRORED = NS(OfPoint=lambda point: xyz(20 - point.X, point.Y + 2, point.Z))


class Link:
    def __init__(self, uid, linked, transform):
        self.UniqueId, self.linked, self.transform = uid, linked, transform
        self.hidden = False

    def IsHidden(self, _view):
        return self.hidden

    def GetLinkDocument(self):
        return self.linked

    def GetTotalTransform(self):
        return self.transform


def model(uid, elements):
    return NS(PathName="/synthetic/" + uid + ".rvt", ProjectInformation=NS(UniqueId=uid),
              IsFamilyDocument=False, GetElement=lambda item: elements.get(item.Value))


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        ElementId=Id, XYZ=xyz, Transform=NS(Identity=IDENTITY),
        ElementType=type("ElementType", (), {}), RevitLinkInstance=Link,
    )))
    reader = runpy.run_path(str(SCRIPTS / "read_clash_bounds.py"))["main"]
    box = NS(Min=xyz(0, 0, 0), Max=xyz(2, 1, 3), Transform=IDENTITY, Enabled=True)
    elem = NS(ViewSpecific=False, Category=object(), UniqueId="same-element-in-two-instances",
              get_BoundingBox=lambda _view: box, IsHidden=lambda _view: False)
    linked = model("linked", {42: elem})
    links = [Link("rotated-instance", linked, ROTATED), Link("mirrored-instance", linked, MIRRORED)]
    host = model("host", {6: links[0], 7: links[1]})
    host.ActiveView = NS(UniqueId="view", IsTemplate=False)
    request = {"active_view_uid": "view", "sources": [
        {"path_prefix": ["Rotated"], "link_instance_id": "6", "link_instance_uid": links[0].UniqueId,
         "element_ids": ["42"]},
        {"path_prefix": ["Mirrored"], "link_instance_id": "7", "link_instance_uid": links[1].UniqueId,
         "element_ids": ["42", "43"]},
    ]}
    return reader, host, request, links


def test_reader_bounds_feed_xml_clash_workflow_with_distinct_link_instances(runtime, tmp_path):
    reader, host, request, _ = runtime
    native = reader(host, request)
    snapshot = json.loads(native["snapshot_json"])
    rotated, mirrored = snapshot["sources"]
    assert rotated["elements"]["42"]["min"] == pytest.approx([10 - COS, 0, 0])
    assert rotated["elements"]["42"]["max"] == pytest.approx([10 + 2 * COS, 3 * COS, 3])
    assert mirrored["elements"]["42"]["min"] == [18, 2, 0]
    assert mirrored["elements"]["42"]["max"] == [20, 3, 3]
    assert mirrored["unresolved"] == {"43": "element_missing"}

    record = tmp_path / "full-run.json"
    record.write_text(json.dumps({"response": {"result": native}}))
    xml = tmp_path / "fixture.xml"
    xml.write_text("""<exchange units="m"><batchtest><clashtests><clashtest><clashresults>
<clashresult guid="11111111-0000-4000-8000-000000000001" name="Synthetic clash" status="new">
<clashobjects>
<clashobject><objectattribute><name>Element ID</name><value>42</value></objectattribute>
<pathlink><node>Rotated</node><node>Fixture element</node></pathlink></clashobject>
<clashobject><objectattribute><name>Element ID</name><value>42</value></objectattribute>
<pathlink><node>Mirrored</node><node>Fixture element</node></pathlink></clashobject>
</clashobjects></clashresult></clashresults></clashtest></clashtests></batchtest></exchange>""")
    out = subprocess.run([sys.executable, str(SCRIPTS / "navisworks_clash_cli.py"), str(xml), "--snapshot", str(record)],
                         check=True, text=True, capture_output=True)
    box = json.loads(out.stdout)["clashes"][0]["section_box"]
    assert box["min"] == pytest.approx([9 - COS, -1, -1])
    assert box["max"] == [21, 4, 4]
    assert [target["model_id"] for target in box["targets"]] == ["link:rotated-instance", "link:mirrored-instance"]
    assert box["targets"][0]["unique_id"] == box["targets"][1]["unique_id"]


def test_reader_reports_missing_visibility_and_refuses_stale_identity(runtime):
    reader, host, request, links = runtime
    links[0].linked = None
    links[1].hidden = True
    sources = json.loads(reader(host, request)["snapshot_json"])["sources"]
    assert sources[0]["elements"] == {} and sources[0]["unresolved"] == {"42": "link_unloaded"}
    assert sources[1]["elements"] == {} and sources[1]["unresolved"] == {
        "42": "link_hidden_in_active_view", "43": "link_hidden_in_active_view",
    }
    request["sources"][1]["link_instance_uid"] = "stale-id"
    with pytest.raises(ValueError, match="changed identity"):
        reader(host, request)
    request["active_view_uid"] = "other-view"
    with pytest.raises(ValueError, match="current view differs"):
        reader(host, request)


def prepare_view(runtime, state="ready"):
    reader, host, request, links = runtime
    guid = "11111111-0000-4000-8000-000000000001"
    view_name = "Clash " + guid
    request["view_request"] = {"clash_guid": guid, "view_type_id": "8"}
    db = reader.__globals__["DB"]
    db.ViewFamilyType = type("ViewFamilyType", (), {})
    db.ViewFamily = NS(ThreeDimensional="3D")
    db.View = type("View", (), {})
    db.NamingUtils = NS(IsValidName=lambda name: name == view_name)
    named = [NS(Name=view_name.upper(), Id=Id(9), UniqueId="prior-view")] if state == "name_collision" else []
    db.FilteredElementCollector = lambda _host: NS(OfClass=lambda _class: named)
    family_type = db.ViewFamilyType()
    family_type.ViewFamily = "3D"
    family_type.UniqueId = "fixture-view-type"
    family_type.DefaultTemplateId = Id(99 if state == "default_template" else -1)
    get_element = host.GetElement
    host.GetElement = lambda item: family_type if item.Value == 8 else get_element(item)
    if state == "unresolved":
        links[1].hidden = True
    return reader, host, request, db, guid, named


@pytest.mark.parametrize("state", ["ready", "name_collision", "default_template", "unresolved"])
def test_section_view_preview_links_two_exact_targets_to_new_view(runtime, tmp_path, state):
    reader, host, request, _, guid, _ = prepare_view(runtime, state)
    view_name = "Clash " + guid
    native = reader(host, request)
    record = tmp_path / "full-run.json"
    record.write_text(json.dumps({"result": native}))
    xml = tmp_path / "fixture.xml"
    objects = "".join(
        '<clashobject><objectattribute><name>Element ID</name><value>42</value></objectattribute>'
        f'<pathlink><node>{prefix}</node><node>Fixture</node></pathlink></clashobject>'
        for prefix in ("Rotated", "Mirrored")
    )
    xml.write_text('<exchange units="m"><batchtest><clashtests><clashtest><clashresults>'
                   f'<clashresult guid="{guid}" name="Fixture clash" status="new">'
                   f'<clashobjects>{objects}</clashobjects></clashresult>'
                   '</clashresults></clashtest></clashtests></batchtest></exchange>')
    output = subprocess.run(
        [sys.executable, str(SCRIPTS / "navisworks_clash_cli.py"), str(xml),
         "--snapshot", str(record), "--view-clash", guid],
        check=True, text=True, capture_output=True,
    )
    result = json.loads(output.stdout)
    plan, display = result["view_plan"], result["view_preview"]
    assert plan["view"]["name"] == view_name
    assert display["basis_sha256"] == plan["basis_sha256"]
    assert display["review_only"] and display["steps"] == [] and display["ownership_ids"] == []
    assert result["plan"] is None
    if state == "ready":
        assert [target["link_instance_id"] for target in plan["targets"]] == ["6", "7"]
        assert [target["element_id"] for target in plan["targets"]] == ["42", "42"]
        assert display["after"][-1][-1] == [21, 4, 4]
        assert plan["steps"] == [{"action": "create_view"}]
    else:
        assert plan["blocked"] and display["blocked"] and plan["steps"] == []
        assert display["warnings"][0]["blocking"]


class ChangeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self


class Transaction:
    status = "uninitialized"

    def __init__(self, host, name):
        self.host, self.name = host, name

    def Start(self):
        self.status = "started"
        return self.status

    def GetStatus(self):
        return self.status

    def RollBack(self):
        self.status = "rolled_back"
        return self.status

    def Commit(self):
        self.status = "committed"
        for handler in self.host.Application.DocumentChanged.handlers:
            event = NS(GetDocument=lambda: self.host, GetTransactionNames=lambda: [self.name],
                       GetAddedElementIds=lambda: self.host.added, GetModifiedElementIds=lambda: self.host.modified,
                       GetDeletedElementIds=lambda: [])
            handler(None, event)
        return self.status


class Group(Transaction):
    def Start(self):
        self.original = list(self.host.views)
        return super().Start()

    def RollBack(self):
        self.host.views[:] = self.original
        return super().RollBack()

    def Assimilate(self):
        return "committed"


class View3D:
    IsTemplate = IsPerspective = IsSectionBoxActive = False

    def __init__(self, type_id):
        self.Id, self.UniqueId, self.type_id, self.ViewTemplateId = Id(100), "view-kept", type_id, Id(-1)
        self.Name = "Default"

    @classmethod
    def CreateIsometric(cls, host, type_id):
        view = cls(type_id)
        host.views.append(view)
        host.added.append(view.Id)
        return view

    def GetTypeId(self):
        return self.type_id

    def SetSectionBox(self, box):
        self.box = box

    def GetSectionBox(self):
        return self.box


@pytest.fixture
def view_writer(runtime):
    from clash_section import section_view_preview

    reader, host, request, db, guid, views = prepare_view(runtime)
    host.views, host.added, host.modified = views, [], []
    host.Application = NS(DocumentChanged=ChangeEvent())
    host.Equals = lambda other: other is host
    host.Regenerate = lambda: None
    db.Transaction, db.TransactionGroup, db.View3D = Transaction, Group, View3D
    db.BoundingBoxXYZ = NS
    db.Transform.Identity.IsIdentity = True
    db.TransactionStatus = NS(Started="started", Pending="pending", Committed="committed", RolledBack="rolled_back")
    get_element = host.GetElement
    host.GetElement = lambda item: next((view for view in host.views if view.Id.Value == item.Value), get_element(item))
    snapshot = json.loads(reader(host, request)["snapshot_json"])
    clash = {"guid": guid, "objects": [{"element_id": "42", "selection_path": ["Rotated", "Fixture"]},
                                      {"element_id": "42", "selection_path": ["Mirrored", "Fixture"]}]}
    plan, display = section_view_preview(guid, [clash], snapshot, 1.0, "a" * 64)
    return reader.__globals__["run"], host, plan, display


def test_guarded_view_creation_verifies_exact_kept_candidate(view_writer):
    run, host, plan, display = view_writer
    result = run(host, {"mode": "apply", "plan": plan, "preview": display})
    assert result["status"] == "ok"
    receipt = json.loads(result["receipt_json"])
    assert receipt["view_id"] == "100" and receipt["view_uid"] == "view-kept"
    assert receipt["view_name"] == plan["view"]["name"]
    assert receipt["box_min"] == plan["box"]["min"] and receipt["box_max"] == plan["box"]["max"]
    assert host.Application.DocumentChanged.handlers == []
    with pytest.raises(ValueError, match="no longer matches"):
        run(host, {"mode": "apply", "plan": plan, "preview": display})
    assert len(host.views) == 1


def test_guarded_view_rolls_back_on_unapproved_change(view_writer):
    run, host, plan, display = view_writer
    host.modified = [Id(1)]
    result = run(host, {"mode": "apply", "plan": plan, "preview": display})
    assert result["status"] == "stopped" and result["target_rollback_verified"]
    assert host.views == [] and host.Application.DocumentChanged.handlers == []


def test_guarded_view_rejects_changed_mapping_or_display_before_writes(view_writer):
    run, host, plan, display = view_writer
    display["after"][-1][-1] = [999, 999, 999]
    with pytest.raises(ValueError, match="displayed clash preview differs"):
        run(host, {"mode": "apply", "plan": plan, "preview": display})
    assert host.views == []
    plan["clash_objects"][0]["selection_path"] = ["Mirrored", "Fixture"]
    with pytest.raises(ValueError, match="no longer matches"):
        run(host, {"mode": "apply", "plan": plan, "preview": display})
    assert host.views == []
