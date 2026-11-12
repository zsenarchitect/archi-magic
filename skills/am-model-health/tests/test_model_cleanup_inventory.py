from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_model_cleanup.py"
PREVIEW = SCRIPT.with_name("cleanup_preview.py")
PROBE = SCRIPT.with_name("probe_model_cleanup_in_revit.py")
VERIFY_PROBE = SCRIPT.with_name("verify_model_cleanup_probe.py")


class Id(int):
    Value = property(int)


class NativeSet(set):
    Add = set.add
    Count = property(len)

    @classmethod
    def __class_getitem__(cls, item):
        return cls


class Collector:
    def __init__(self, document, view_id=None):
        self.document = document
        self.view_id = view_id
        self.items = document.groups.get((view_id, "graphics"), [])

    @staticmethod
    def IsViewValidForElementIteration(document, view_id):
        return view_id not in document.unsupported_views

    def OfClass(self, cls):
        key = (self.view_id, cls) if self.view_id is not None else cls
        self.items = self.document.groups.get(key, [])
        return self

    OfCategory = OfClass

    def OwnedByView(self, view_id):
        self.items = self.document.groups.get((view_id, "owned"), [])
        return self

    def WhereElementIsNotElementType(self):
        return self

    def __iter__(self):
        return iter(self.items)

    def Dispose(self):
        pass


def item(identifier, category=10, **fields):
    return NS(Id=Id(identifier), UniqueId="uid-{}".format(identifier), VersionGuid="version",
              Category=NS(Id=Id(category)) if category is not None else None, Pinned=False,
              GetDependentElements=lambda _: [Id(9007199254740993)], **fields)


def view(identifier, markers=(), template=False, filters=()):
    return item(identifier, IsTemplate=template, ViewTemplateId=Id(-1), ViewType="DraftingView",
                get_Parameter=lambda _: None, AreGraphicsOverridesAllowed=lambda: True,
                GetFilters=lambda: map(Id, filters), GetPrimaryViewId=lambda: Id(-1),
                GetReferenceCallouts=lambda: map(Id, markers), GetReferenceSections=list,
                GetReferenceElevations=list)


@pytest.mark.parametrize("target_exists, blocked_view", [(True, None), (False, None), (True, Id(2))])
def test_query_reports_exact_native_ids_and_sheet_template_references(monkeypatch, target_exists, blocked_view):
    db = NS(FilteredElementCollector=Collector, ElementId=Id, View="views", Viewport="ports",
            ScheduleSheetInstance="schedules", Electrical=NS(PanelScheduleSheetInstance="panels"),
            ViewFamilyType="view_types", DatumPlane="datums", ParameterFilterElement="filters",
            SelectionFilterElement="selection_filters", ImportInstance="cad",
            BuiltInCategory=NS(OST_VolumeOfInterest="scope", INVALID=-1),
            BuiltInParameter=NS(VIEWER_VOLUME_OF_INTEREST_CROP=1, DATUM_VOLUME_OF_INTEREST=2),
            ReferenceableViewUtils=NS(GetReferencedViewId=lambda doc, marker: Id(2)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(HashSet=NativeSet))
    clock = NS(Elapsed=NS(TotalSeconds=0))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: clock)))
    parent, target = view(1, markers=[90]), view(2)
    template = view(3, template=True, filters=[5])
    imported = item(6, category=17, OwnerViewId=Id(1), IsLinked=False, ViewSpecific=True)
    model_wide = item(7, category=17, OwnerViewId=Id(-1), IsLinked=False, ViewSpecific=False)
    groups = {"views": [parent, target, template, view(4)],
              "ports": [NS(ViewId=Id(1), SheetId=Id(10))],
              "panels": [NS(ScheduleId=Id(4), OwnerViewId=Id(10))],
              "filters": [item(5, category=None)],
              "cad": [imported, model_wide],
              (Id(1), "owned"): [imported],
              (Id(1), "graphics"): [parent, model_wide],
              (Id(1), "cad"): [model_wide],
              (Id(4), "owned"): [item(9007199254740993)]}

    category_calls = []

    def unused(categories):
        category_calls.append(set(categories))
        return [Id(2), Id(5), Id(600)]

    document = NS(IsFamilyDocument=False, Application=NS(VersionNumber="2026"), ActiveView=parent,
                  groups=groups, unsupported_views={blocked_view}, GetUnusedElements=unused,
                  PathName="Synthetic.rvt", ProjectInformation=NS(UniqueId="project-uid"),
                  GetElement=lambda _: target if target_exists else None)
    namespace = runpy.run_path(str(SCRIPT), init_globals={"doc": document})
    result = namespace["result"]
    assert category_calls == [{Id(10), Id(17), Id(-1)}]
    assert result["document"] == {"project_uid": "project-uid", "active_view_id": "1", "active_view_uid": "uid-1",
                                  "path_sha256": hashlib.sha256(b"Synthetic.rvt").hexdigest()}
    rows = {row["id"]: row for row in result["rows"]}
    assert set(rows) == {"1", "2", "3", "4", "5", "6", "7"}
    assert all(row["disposition"] == "report_only" for row in rows.values())
    assert rows["2"]["purge_unused"] is True
    assert rows["5"]["purge_unused"] is True
    assert rows["6"]["purge_unused"] is False
    assert rows["6"]["dependent_ids"] == ["9007199254740993"]
    assert {ref["kind"] for ref in rows["6"]["references"]} == {"active_view", "viewport"}
    assert rows["4"]["references"] == [{"source_id": "10", "kind": "panel_schedule"}]
    assert rows["1"]["content"] == {"status": "observed", "owned_example_id": "6", "graphic_example_id": "7"}
    assert rows["4"]["content"]["owned_example_id"] == "9007199254740993"
    assert rows["6"]["potentially_visible_in"] == []
    assert rows["6"]["owner_graphics_checked"] is True
    assert rows["7"]["potentially_visible_in"] == ["1"]
    assert rows["7"]["owner_graphics_checked"] is False
    assert result["graphic_coverage"]["checked_graphic_views"] == (2 if blocked_view else 3)
    if blocked_view:
        assert rows["2"]["content"] == {"status": "incomplete", "owned_example_id": None}
        assert result["incomplete"] == [{"group": "graphics", "reason": "unsupported_drafting_view"}]
    else:
        assert rows["2"]["content"] == {"status": "none_observed", "owned_example_id": None,
                                       "graphic_example_id": None}
    if target_exists:
        assert result["status"] == ("incomplete" if blocked_view else "reported")
        assert rows["2"]["references"] == [{"source_id": "1", "kind": "reference_view"}]
        assert rows["5"]["references"] == [{"source_id": "3", "kind": "attached_filter"}]
    else:
        assert result["status"] == "incomplete"
        assert result["incomplete"][0]["group"] == "references"

    if target_exists and not blocked_view:
        read_inventory = namespace["read_inventory"]
        monkeypatch.setitem(read_inventory.__globals__, "MAX_ROWS", 1)
        broad = read_inventory(document)
        assert broad["status"] == "incomplete"
        assert {"group": "rows", "reason": "row_limit"} in broad["incomplete"]
        narrow = read_inventory(document, ["5"])
        assert narrow["status"] == "reported"
        assert narrow["scope"]["model_candidate_count"] == 7
        assert narrow["rows"] == [rows["5"]]
        assert category_calls[-1] == {Id(-1)}
        protected = narrow["scope"]["protection_sha256"]
        template.UniqueId = "replacement-template"
        assert read_inventory(document, ["5"])["scope"]["protection_sha256"] != protected
        template.UniqueId = "uid-3"
        groups["panels"] = []
        assert read_inventory(document, ["5"])["scope"]["protection_sha256"] != protected
        with pytest.raises(ValueError, match="unique positive decimal-string"):
            read_inventory(document, ["5", "5"])

        owner = view(8)
        groups["views"].append(owner)
        imported.OwnerViewId = owner.Id
        imported.VersionGuid = "12345678-1234-1234-1234-123456789abc"
        imported.GetDependentElements = lambda _: []
        document.GetUnusedElements = lambda _: [Id(6)]
        build = runpy.run_path(str(PREVIEW))["build_preview"]
        control = read_inventory(document, ["6"])
        assert build(control, ["6"])["cleanup_selection"]["probe_candidate_ids"] == ["6"]
        owner.ViewType = "DrawingSheet"
        sheet_inventory = read_inventory(document, ["6"])
        assert sheet_inventory["rows"][0]["references"] == [{"source_id": "8", "kind": "sheet_owned_import"}]
        preview = build(sheet_inventory, ["6"])
        assert preview["cleanup_selection"]["probe_candidate_ids"] == []
        assert any("owned by a sheet" in warning["text"] for warning in preview["warnings"])


def cleanup_preview_fixture():
    row = {"id": "9007199254740993", "uid": "filter-uid", "kind": "filter",
           "version_guid": "12345678-1234-1234-1234-123456789abc", "pinned": False,
           "references": [], "dependent_ids": [], "purge_unused": True}
    document = {"project_uid": "project", "active_view_id": "30", "active_view_uid": "view",
                "path_sha256": "a" * 64}
    return {"kind": "revit-cleanup-inventory", "status": "reported", "incomplete": [],
            "document": document, "candidate_count": 1, "rows": [row]}


def test_cleanup_preview_binds_selected_ids_and_never_emits_delete_steps():
    build = runpy.run_path(str(PREVIEW))["build_preview"]
    inventory = cleanup_preview_fixture()
    identifier = inventory["rows"][0]["id"]
    preview = build(inventory, [identifier])
    assert preview["after"] == [[identifier, "filter", "Needs native verification"]]
    assert preview["blocked"] is True and preview["review_only"] is True and preview["steps"] == []
    assert preview["cleanup_selection"] == {"document": inventory["document"], "selected_ids": [identifier],
                                             "probe_candidate_ids": [identifier], "elements": [
        {"id": identifier, "kind": "filter", "uid": "filter-uid",
         "version_guid": "12345678-1234-1234-1234-123456789abc"}]}
    assert build(inventory, ["10", identifier])["basis_sha256"] != preview["basis_sha256"]
    assert build(inventory, ["10", identifier])["basis_sha256"] == build(inventory, [identifier, "10"])["basis_sha256"]
    inventory["document"]["active_view_id"] = "31"
    assert build(inventory, [identifier])["basis_sha256"] != preview["basis_sha256"]
    with pytest.raises(ValueError, match="unique"):
        build(inventory, [identifier, identifier])


@pytest.mark.parametrize("change,reason", [
    ({"references": [{"kind": "reference_view", "source_id": "10"}]}, "sheet placement or view reference"),
    ({"references": [{"kind": "attached_filter", "source_id": "10"}]}, "has references"),
    ({"dependent_ids": ["40"]}, "has dependents"),
    ({"purge_unused": None}, "did not confirm this exact ID"),
    ({"kind": "view", "view_type": "DraftingView", "content": {"status": "observed"}}, "view has content"),
    ({"kind": "cad", "linked": False, "view_specific": True, "owner_view_id": "30",
      "potentially_visible_in": ["30"]}, "may be visible"),
])
def test_cleanup_preview_explains_protected_selections(change, reason):
    build = runpy.run_path(str(PREVIEW))["build_preview"]
    inventory = cleanup_preview_fixture()
    row = inventory["rows"][0]
    row.update(change)
    preview = build(inventory, [row["id"], "11"])
    assert preview["after"] == [["11", "unknown", "Blocked"], [row["id"], row["kind"], "Blocked"]]
    assert any(reason in warning["text"] and warning["row"] == row["id"] for warning in preview["warnings"])
    assert preview["steps"] == []


def test_cleanup_preview_blocks_apparently_clear_rows_from_incomplete_inventory():
    build = runpy.run_path(str(PREVIEW))["build_preview"]
    inventory = cleanup_preview_fixture()
    inventory["candidate_count"] = 2
    identifier = inventory["rows"][0]["id"]
    preview = build(inventory, [identifier])
    assert preview["after"] == [[identifier, "filter", "Blocked"]]
    assert "inventory is incomplete" in preview["warnings"][0]["text"]


def test_selected_cleanup_inventory_requires_matching_ids_and_protection_digest():
    build = runpy.run_path(str(PREVIEW))["build_preview"]
    inventory = cleanup_preview_fixture()
    identifier = inventory["rows"][0]["id"]
    inventory["scope"] = {"selected_ids": [identifier], "protection_sha256": "a" * 64}
    assert build(inventory, [identifier])["cleanup_selection"]["scope"] == "selected"
    with pytest.raises(ValueError, match="same IDs"):
        build(inventory, ["11"])
    inventory["scope"]["protection_sha256"] = None
    blocked = build(inventory, [identifier])
    assert blocked["cleanup_selection"]["probe_candidate_ids"] == []
    assert "model-wide protection evidence" in blocked["warnings"][0]["text"]


@pytest.mark.parametrize("scenario,status", [
    ("exact", "probe_rolled_back"),
    ("scoped", "probe_rolled_back"),
    ("cascade", "stopped"),
    ("modified", "stopped"),
    ("no_event", "stopped"),
    ("other_document", "transaction_unresolved"),
    ("warning", "stopped"),
    ("drift", "preflight"),
    ("unrestored", "transaction_unresolved"),
])
def test_cleanup_probe_rolls_back_and_checks_native_effects(monkeypatch, scenario, status):
    inventory = cleanup_preview_fixture()
    row = inventory["rows"][0]
    selected_id = row["id"]
    native_id = Id(selected_id)
    selected = item(native_id)
    selected.UniqueId, selected.VersionGuid = row["uid"], row["version_guid"]
    selected.GetDependentElements = lambda _: []
    elements = {native_id: selected, Id(30): item(30)}
    events = []
    started = []

    class Event:
        def __iadd__(self, callback):
            events.append(callback)
            return self

        def __isub__(self, callback):
            events.remove(callback)
            return self

    application = NS(DocumentChanged=Event())
    document = NS(Application=application, IsReadOnly=False, IsModifiable=False, IsWorkshared=False,
                  GetElement=elements.get, Equals=lambda other: other is document)

    def delete(identifier):
        del elements[identifier]
        if scenario == "cascade":
            del elements[Id(30)]
            return [identifier, Id(30)]
        return [identifier]

    document.Delete = delete
    states = NS(**{name: name for name in ("Uninitialized", "Started", "Committed", "RolledBack")})
    failure_results = NS(ProceedWithRollBack="rollback", Continue="continue")

    class Options:
        def SetFailuresPreprocessor(self, value):
            self.guard = value

        def SetClearAfterRollback(self, value):
            self.clear = value

        def GetFailuresPreprocessor(self):
            return self.guard

        def GetClearAfterRollback(self):
            return self.clear

        def Dispose(self):
            pass

    class Group:
        def __init__(self, _, name):
            self.name = name
            self.state = states.Uninitialized

        def Start(self):
            started.append(self)
            self.before = dict(elements)
            self.state = states.Started
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            if scenario != "unrestored":
                elements.update(self.before)
            self.state = states.RolledBack
            return self.state

    class Transaction(Group):
        def __init__(self, document, name):
            super().__init__(document, name)
            self.options = Options()

        def GetFailureHandlingOptions(self):
            return self.options

        def SetFailureHandlingOptions(self, options):
            self.options = options

        def Commit(self):
            failures = [object()] if scenario == "warning" else []
            decision = self.options.guard.PreprocessFailures(NS(GetFailureMessages=lambda: failures))
            if decision == failure_results.ProceedWithRollBack:
                return self.RollBack()
            self.state = states.Committed
            if scenario != "no_event":
                event_document = NS(Equals=lambda _: False) if scenario == "other_document" else document
                event = NS(Operation="TransactionCommitted", GetDocument=lambda: event_document,
                           GetTransactionNames=lambda: [self.name], GetAddedElementIds=list,
                           GetModifiedElementIds=lambda: [Id(30)] if scenario == "modified" else [],
                           GetDeletedElementIds=lambda: [native_id])
                for callback in list(events):
                    callback(None, event)
            return self.state

    if scenario == "scoped":
        inventory["scope"] = {"selected_ids": [selected_id], "protection_sha256": "a" * 64}

    def read_inventory(_document, selection=None):
        assert selection == ([selected_id] if scenario == "scoped" else None)
        return inventory if native_id in elements and Id(30) in elements else dict(inventory, status="incomplete")

    build = runpy.run_path(str(PREVIEW))["build_preview"]
    monkeypatch.setitem(sys.modules, "cleanup_preview", NS(build_preview=build))
    monkeypatch.setitem(sys.modules, "read_model_cleanup", NS(
        check_time=lambda _: None, element_id=lambda identifier: str(identifier.Value), read_inventory=read_inventory))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=NS(
        ElementId=Id, TransactionGroup=Group, Transaction=Transaction, TransactionStatus=states,
        IFailuresPreprocessor=object, FailureProcessingResult=failure_results)))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=object)))
    probe = runpy.run_path(str(PROBE))["probe"]
    request = {"mode": "rollback_probe", "selected_ids": [selected_id],
               "basis_sha256": build(inventory, [selected_id])["basis_sha256"]}
    if scenario == "scoped":
        request["scope"] = "selected"
    if scenario == "drift":
        inventory["document"]["active_view_id"] = "31"
        with pytest.raises(ValueError, match="selection changed"):
            probe(document, request)
        assert started == []
        return

    result = probe(document, request)
    assert result["status"] == status
    assert events == []
    assert [step.GetStatus() for step in started][0] == states.RolledBack
    if status == "transaction_unresolved":
        assert selected_id not in result.get("temporary_deleted_ids", [])
        return
    assert result["rollback_verified"] is True
    assert set(elements) == {native_id, Id(30)}
    if status == "probe_rolled_back":
        assert result["temporary_deleted_ids"] == [selected_id]
        assert result["transaction"] == started[1].name


def test_verify_cleanup_probe_requires_exact_selection_and_independent_records(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    verify = runpy.run_path(str(VERIFY_PROBE))["verify"]
    build = runpy.run_path(str(PREVIEW))["build_preview"]
    inventory = cleanup_preview_fixture()
    identifier = inventory["rows"][0]["id"]
    preview = build(inventory, [identifier])

    def record(run_id, mode, result):
        return {"run_id": run_id, "mode": mode, "status": "ok", "decision": "rolled_back",
                "exit_code": 0, "result_truncated": False, "error": None, "approval": None,
                "warnings": [], "dialogs": [], "failures": [], "blocked": [],
                "engine": {"requested": "cpython"}, "result": deepcopy(result),
                "changes": {"added_count": 0, "modified_count": 0, "deleted_count": 0}}

    before = record("before", "query", inventory)
    fresh = record("fresh", "query", inventory)
    transaction = "Probe Revit cleanup " + "a" * 32
    dry = record("probe", "dry_run", {"status": "probe_rolled_back", "rollback_verified": True,
                                      "basis_sha256": preview["basis_sha256"], "document": inventory["document"],
                                      "temporary_deleted_ids": [identifier], "transaction": transaction})
    dry["changes"] = {"added_count": 1, "modified_count": 0, "deleted_count": 1,
                      "added": [{"id": int(identifier)}], "modified": [], "deleted": [int(identifier)],
                      "transactions": [transaction], "by_category": {"Filters": 1}}
    paths = [tmp_path / (name + ".json") for name in ("preview", "before", "probe", "fresh")]
    for path, value in zip(paths, (preview, before, {"response": dry}, fresh)):
        path.write_text(json.dumps(value), encoding="utf-8")
    completed = subprocess.run([sys.executable, str(VERIFY_PROBE), *map(str, paths)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "status": "rollback_probe_verified", "basis_sha256": preview["basis_sha256"], "before_run_id": "before",
        "dry_run_id": "probe", "fresh_run_id": "fresh", "temporary_deleted_ids": [identifier],
        "document": inventory["document"],
    }
    with pytest.raises(ValueError, match="separate Revit runs"):
        verify(preview, before, dry, dict(fresh, run_id="before"))
    with pytest.raises(ValueError, match="not attributed solely"):
        verify(preview, before, dict(dry, changes=dict(dry["changes"], other_documents=["other"])), fresh)
    with pytest.raises(ValueError, match="complete successful Revit record"):
        verify(preview, before, dict(dry, result_truncated=True), fresh)
    with pytest.raises(ValueError, match="exact reviewed deletion"):
        verify(preview, before, dict(dry, result=dict(dry["result"], temporary_deleted_ids=["11"])), fresh)
    with pytest.raises(ValueError, match="outside the reviewed selection"):
        verify(preview, before, dict(dry, changes=dict(dry["changes"], added=[{"id": 11}])), fresh)
    with pytest.raises(ValueError, match="not attributed solely"):
        verify(preview, before, dict(dry, changes=dict(dry["changes"], transactions=["Other"])), fresh)
    with pytest.raises(ValueError, match="change samples are incomplete"):
        verify(preview, before, dict(dry, changes=dict(dry["changes"], deleted=[])), fresh)
    clean = dict(dry, decision="no_changes", changes={"added_count": 0, "modified_count": 0, "deleted_count": 0})
    assert verify(preview, before, clean, fresh)["status"] == "rollback_probe_verified"
    drifted = deepcopy(fresh)
    drifted["result"]["rows"][0]["uid"] = "replacement-filter"
    with pytest.raises(ValueError, match="inventory differs after rollback"):
        verify(preview, before, dry, drifted)
    tampered = dict(preview, steps=[{"delete": identifier}])
    with pytest.raises(ValueError, match="reviewed preview differs"):
        verify(tampered, before, dry, fresh)
