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


def test_clash_snapshot_digest_preserves_exact_floating_point_evidence():
    from clash_section import snapshot_sha256

    # This vector produced the same digest in CPython and IronPython 2.7.12.
    snapshot = {"min": [1e-8, -0.0, 9.292893218813452], "id": "9007199254740993",
                "name": "fixture-\u03a9"}
    digest = "79630270d421dc98b7a0c6b27246e9eb5ff692b98188fac1bb4fe753b72d7bf8"
    assert snapshot_sha256(snapshot) == digest
    snapshot["min"][0] = math.nextafter(1e-8, math.inf)
    assert snapshot_sha256(snapshot) != digest
    snapshot["min"][0] = math.nan
    with pytest.raises(ValueError, match="nonfinite"):
        snapshot_sha256(snapshot)


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
    command = [sys.executable, str(SCRIPTS / "navisworks_clash_cli.py"), str(xml),
               "--snapshot", str(record), "--view-clash", guid]
    output = subprocess.run(command, check=True, text=True, capture_output=True)
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
        receipt = {"clash_guid": guid, "view_name": view_name, "view_id": "100", "view_uid": "fixture-view",
                   "view_type_id": plan["view"]["view_type_id"], "basis_sha256": plan["basis_sha256"],
                   "host_identity": plan["host_identity"], "box_min": plan["box"]["min"], "box_max": plan["box"]["max"]}
        unchanged = {"added_count": 0, "modified_count": 0, "deleted_count": 0,
                     "added": [], "modified": [], "deleted": []}
        dry_run = tmp_path / "dry-run.json"
        dry_run.write_text(json.dumps({"mode": "dry_run", "status": "ok", "decision": "rolled_back",
                                      "exit_code": 0, "result_truncated": False,
                                      "run_id": "dry-run", "result": {"status": "ok", "receipt_json": json.dumps(receipt)},
                                      "changes": dict(unchanged, added_count=1, added=[{"id": 100, "class": "View3D"}])}))
        fresh = tmp_path / "fresh.json"
        rollback = {"snapshot": json.loads(native["snapshot_json"]),
                    "confirmed_absent": {"id": receipt["view_id"], "uid": receipt["view_uid"]}}
        fresh.write_text(json.dumps({"response": {"mode": "query", "status": "ok", "decision": "rolled_back",
                                                 "exit_code": 0, "result_truncated": False, "run_id": "readback",
                                                 "result": {"status": "ok", "rollback_json": json.dumps(rollback)},
                                                 "changes": unchanged}}))
        verified = subprocess.run(command + ["--verify-view", "dry-run", "--view-run", str(dry_run),
                                             "--view-readback", str(fresh)],
                                  check=True, text=True, capture_output=True)
        assert json.loads(verified.stdout)["view_verification"] == {"status": "verified_dry_run"}

        import issue_board as ib
        keep = tmp_path / "keep.json"
        keep.write_text(json.dumps(dict(json.loads(dry_run.read_text()), mode="modify", decision="committed",
                                        approval="user")))
        response = json.loads(fresh.read_text())["response"]
        response["result"] = {"status": "ok", "verification_json": json.dumps({
            "view": receipt, "potentially_visible": plan["targets"],
        })}
        fresh.write_text(json.dumps(response))
        note = tmp_path / "board.json"
        board_body = ib.plan_create("Fixture", [])["final_text"]
        target = {"id": "150", "unique_id": "board-note"}

        def write_board(body, host_identity=plan["host_identity"]):
            note.write_text(json.dumps({"host_identity": host_identity,
                                       "note": dict(target, text=body, sha1=ib.text_sha1(body))}))

        write_board(board_body)
        handoff = command + ["--verify-view", "keep", "--view-run", str(keep), "--view-readback",
                             str(fresh), "--board", str(note)]
        refused = subprocess.run(handoff, capture_output=True, text=True)
        assert refused.returncode == 2 and not refused.stdout
        handoff.append("--confirm-visible")

        def proposal():
            return json.loads(subprocess.run(handoff, check=True, capture_output=True, text=True).stdout)

        board_result = json.loads(note.read_text())
        board_query = dict(response, run_id="board-baseline", result=board_result)
        for bad_query in (dict(board_query, status="error"),
                          dict(board_query, result_truncated=True),
                          dict(board_query, changes=dict(unchanged, modified_count=1,
                                                         modified=[{"id": 150, "class": "TextNote"}]))):
            note.write_text(json.dumps({"response": bad_query}))
            refused = subprocess.run(handoff, capture_output=True, text=True)
            assert refused.returncode == 2 and not refused.stdout
        note.write_text(json.dumps({"response": board_query}))
        proposed = proposal()
        board_plan = proposed["plan"]
        assert proposed["board_handoff"] == "planned"
        assert board_plan["structural_target"]["active"] == dict(target, sha1=ib.text_sha1(board_body))
        assert board_plan["structural_target"]["view"]["view_uid"] == "fixture-view"
        confirmed_issue, = ib.parse_board(board_plan["final_text"])["issues"]
        assert confirmed_issue["findings"] == [
            "Navisworks clash UUID: " + guid, "Verified Revit section view " + view_name + ".",
        ]
        board_run = tmp_path / "board-keep.json"
        board_run.write_text(json.dumps({
            "mode": "modify", "status": "ok", "decision": "committed", "approval": "user",
            "exit_code": 0, "result_truncated": False,
            "run_id": "board-keep", "result": {"status": "ok", "note_id": 150,
                                               "sha1_after": ib.text_sha1(board_plan["final_text"])},
            "changes": dict(unchanged, modified_count=1, modified=[{"id": 150, "class": "TextNote"}]),
        }))
        board_readback = tmp_path / "board-fresh.json"
        write_board(board_plan["final_text"])
        board_readback.write_text(json.dumps({
            "mode": "query", "status": "ok", "decision": "rolled_back", "run_id": "board-query",
            "exit_code": 0, "result_truncated": False,
            "result": json.loads(note.read_text()), "changes": unchanged,
        }))
        write_board(board_body)
        final = subprocess.run(handoff + ["--verify-board", "keep", "--board-run", str(board_run),
                                         "--board-readback", str(board_readback)],
                               check=True, capture_output=True, text=True)
        assert json.loads(final.stdout)["board_verification"] == {"status": "verified_keep", "note_id": "150"}
        write_board(board_plan["final_text"])
        retry = proposal()
        assert retry["board_handoff"] == "already_recorded" and retry["plan"] is None
        previously_imported = board_plan["final_text"].replace(confirmed_issue["findings"][1],
                                                               "Section bounds proposed for review.")
        write_board(previously_imported)
        updated = proposal()["plan"]["final_text"]
        assert ib.parse_board(updated)["issues"] == [confirmed_issue]
        write_board(board_body, {"project_uid": "another-project"})
        refused = subprocess.run(handoff, capture_output=True, text=True)
        assert refused.returncode == 2 and not refused.stdout
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
    host.GetElement = lambda item: (
        next((view for view in host.views if view.UniqueId == item), None) if isinstance(item, str)
        else next((view for view in host.views if view.Id.Value == item.Value), get_element(item))
    )
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


@pytest.mark.parametrize("state", ["ready", "wrong_view", "changed_box", "missing_link_target"])
def test_verify_kept_view_checks_both_link_instances(view_writer, state):
    run, host, plan, display = view_writer
    receipt = json.loads(run(host, {"mode": "apply", "plan": plan, "preview": display})["receipt_json"])
    view = host.views[0]
    host.ActiveView = NS(Id=Id(101), UniqueId="other") if state == "wrong_view" else view
    if state == "changed_box":
        view.box.Min = xyz(0, 0, 0)
    opened, disposed = [], []

    def collector(document, view_id, link_id):
        assert document is host and view_id.Value == 100
        opened.append(link_id.Value)
        element = host.GetElement(link_id).GetLinkDocument().GetElement(Id(42))
        element.Id = Id(42)
        visible = [] if state == "missing_link_target" and link_id.Value == 7 else [element]
        return NS(WhereElementIsNotElementType=lambda: visible, Dispose=lambda: disposed.append(link_id.Value))

    run.__globals__["DB"].FilteredElementCollector = collector
    args = {"mode": "verify", "plan": plan, "receipt": receipt}
    if state == "ready":
        evidence = json.loads(run(host, args)["verification_json"])
        assert evidence == {"view": receipt, "potentially_visible": plan["targets"]}
    else:
        messages = {"wrong_view": "Activate the exact kept clash view",
                    "changed_box": "section box differs",
                    "missing_link_target": "potentially visible"}
        with pytest.raises(ValueError, match=messages[state]):
            run(host, args)
    assert opened == disposed == ([6, 7] if state in ("ready", "missing_link_target") else [])
    assert host.views == [view]


@pytest.mark.parametrize("outcome,mode,status,decision,approval", [
    ("dry-run", "dry_run", "ok", "rolled_back", None),
    ("discard", "modify", "rejected", "discarded", "user"),
    ("keep", "modify", "ok", "committed", "user"),
])
def test_verify_view_run_requires_matching_runtime_and_readback(view_writer, outcome, mode, status, decision, approval):
    from navisworks_clash_cli import verify_view_run

    run, host, plan, display = view_writer
    applied = run(host, {"mode": "apply", "plan": plan, "preview": display})
    receipt = json.loads(applied["receipt_json"])
    unchanged = {"added_count": 0, "modified_count": 0, "deleted_count": 0,
                 "added": [], "modified": [], "deleted": []}
    created = dict(unchanged, added_count=1, added=[{"id": 100, "class": "View3D"}])
    record = {"mode": mode, "status": status, "decision": decision, "approval": approval,
              "run_id": "create-run", "changes": created, "result": applied,
              "exit_code": 0, "result_truncated": False}
    if outcome == "keep":
        evidence = {"status": "ok", "verification_json": json.dumps({
            "view": receipt, "potentially_visible": plan["targets"],
        })}
    else:
        host.views.clear()
        evidence = run(host, {"mode": "verify-rollback", "plan": plan, "receipt": receipt})
    readback = {"mode": "query", "status": "ok", "decision": "rolled_back", "run_id": "readback-run",
                "changes": unchanged, "result": evidence, "exit_code": 0, "result_truncated": False}
    result = verify_view_run(plan, outcome, record, readback)
    assert result["status"] == "verified_" + outcome.replace("-", "_")
    if outcome == "keep":
        assert result["view"] == receipt and result["visual_inspection_required"] is True
        evidence = dict(evidence, verification_json=json.dumps({"view": receipt, "potentially_visible": []}))
    else:
        rollback = json.loads(evidence["rollback_json"])
        rollback["confirmed_absent"]["uid"] = "other"
        evidence = dict(evidence, rollback_json=json.dumps(rollback))
    with pytest.raises(ValueError, match="readback changed|both mapped targets"):
        verify_view_run(plan, outcome, record, dict(readback, result=evidence))
    with pytest.raises(ValueError, match="only the exact added"):
        verify_view_run(plan, outcome, dict(record, changes=dict(created, added=[{"id": 101, "class": "View3D"}])),
                        readback)
    with pytest.raises(ValueError, match="independent readback"):
        verify_view_run(plan, outcome, record, dict(readback, run_id=record["run_id"]))
    with pytest.raises(ValueError, match="does not establish"):
        verify_view_run(plan, outcome, record, dict(readback, result_truncated=True))


@pytest.mark.parametrize("identity", ["id", "uid"])
def test_verify_rollback_refuses_a_renamed_view_by_either_identity(view_writer, identity):
    run, host, plan, display = view_writer
    receipt = json.loads(run(host, {"mode": "apply", "plan": plan, "preview": display})["receipt_json"])
    host.views[0].Name = "Renamed view"
    if identity == "uid":
        host.views[0].Id = Id(101)
    else:
        host.views[0].UniqueId = "different"
    with pytest.raises(ValueError, match="still resolves by ID or UID"):
        run(host, {"mode": "verify-rollback", "plan": plan, "receipt": receipt})


@pytest.mark.parametrize("change", ["valid", "view_replaced", "box_changed"])
def test_board_apply_checks_kept_view_before_transaction(view_writer, change):
    import issue_board as ib
    from navisworks_clash_cli import kept_board_preview

    run, host, plan, display = view_writer
    receipt = json.loads(run(host, {"mode": "apply", "plan": plan, "preview": display})["receipt_json"])
    body = ib.plan_create("Fixture", [])["final_text"]
    source = {"host_identity": plan["host_identity"], "note": {
        "id": "150", "unique_id": "board-note", "text": body, "sha1": ib.text_sha1(body),
    }}
    clash = {"guid": receipt["clash_guid"], "name": "Fixture clash", "section_box": plan["box"]}
    board_plan, _, status = kept_board_preview(source, None, clash, receipt)
    assert status == "planned"

    class TextNote:
        UniqueId = "board-note"

        def GetFormattedText(self):
            return NS(GetPlainText=lambda: body)

    db = run.__globals__["DB"]
    db.TextNote = TextNote
    view = host.views[0]
    if change == "view_replaced":
        view.UniqueId = "another-view"
    elif change == "box_changed":
        view.box.Min = xyz(0, 0, 0)
    get_element = host.GetElement
    host.GetElement = lambda item: TextNote() if item.Value == 150 else get_element(item)

    def transaction(*_):
        raise RuntimeError("transaction entry reached")

    db.Transaction = transaction
    globals_ = {"inputs": {"plan": board_plan, "note_id": "150"},
                "__revit__": NS(ActiveUIDocument=NS(Document=host))}
    expected, message = {
        "valid": (RuntimeError, "transaction entry reached"),
        "view_replaced": (ValueError, "verified clash view changed"),
        "box_changed": (ValueError, "verified clash section box changed"),
    }[change]
    with pytest.raises(expected, match=message):
        runpy.run_path(str(SCRIPTS / "apply_board.py"), init_globals=globals_)


@pytest.mark.parametrize("outcome,mode,status,decision,approval", [
    ("dry-run", "dry_run", "ok", "rolled_back", None),
    ("discard", "modify", "rejected", "discarded", "user"),
    ("keep", "modify", "ok", "committed", "user"),
])
def test_board_run_rejects_wrong_note_side_effects_and_stale_readback(outcome, mode, status, decision, approval):
    import issue_board as ib
    from navisworks_clash_cli import verify_board_run

    host = {"project_uid": "fixture", "path_sha256": "a" * 64}
    before = ib.plan_create("Fixture", [])["final_text"]
    plan = ib.plan_add_issue(before, {"title": "Clash fixture"})
    note = {"id": "150", "unique_id": "note-fixture", "sha1": ib.text_sha1(before)}
    plan["structural_target"] = {"host_identity": host, "active": note}
    after = plan["final_text"]
    after_digest = ib.text_sha1(after)
    observed = after if outcome == "keep" else before
    fresh = {"host_identity": host, "note": dict(note, text=observed, sha1=ib.text_sha1(observed))}
    unchanged = {"added_count": 0, "modified_count": 0, "deleted_count": 0,
                 "added": [], "modified": [], "deleted": []}
    edited = dict(unchanged, modified_count=1, modified=[{"id": 150, "class": "TextNote"}])
    record = {"run_id": "board-run", "mode": mode, "status": status, "decision": decision,
              "approval": approval, "changes": edited, "exit_code": 0, "result_truncated": False,
              "result": {"status": "ok", "note_id": 150, "sha1_after": after_digest}}
    readback = {"run_id": "board-readback", "mode": "query", "status": "ok", "decision": "rolled_back",
                "changes": unchanged, "result": fresh, "exit_code": 0, "result_truncated": False}
    assert verify_board_run(plan, outcome, record, readback) == {
        "status": "verified_" + outcome.replace("-", "_"), "note_id": "150",
    }
    wrong_note = dict(fresh, note=dict(fresh["note"], unique_id="same-text-other-note"))
    with pytest.raises(ValueError, match="text or identity"):
        verify_board_run(plan, outcome, record, dict(readback, result=wrong_note))
    wrong_text = before if outcome == "keep" else after
    stale = dict(fresh, note=dict(fresh["note"], text=wrong_text, sha1=ib.text_sha1(wrong_text)))
    with pytest.raises(ValueError, match="text or identity"):
        verify_board_run(plan, outcome, record, dict(readback, result=stale))
    with pytest.raises(ValueError, match="only the reviewed text note"):
        verify_board_run(plan, outcome, dict(record, changes=dict(edited, added_count=1,
                         added=[{"id": 300, "class": "TextNote"}])), readback)
