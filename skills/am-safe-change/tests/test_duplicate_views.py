import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import view_duplicate_preview as preview


class Id:
    def __init__(self, value):
        self.Value = value


class View:
    VersionGuid = "fixed"
    ViewType = "FloorPlan"
    Parameters = []

    def __init__(self, key, name, is_template=False):
        self.Id, self.Name, self.IsTemplate = Id(key), name, is_template
        self.UniqueId = "view-%d" % key
        self.ViewTemplateId = Id(-1)
        self.Scale = 100
        self.controls = [Id(-1)]
        self.children = []
        self.parent = Id(-1)

    def GetTemplateParameterIds(self):
        return self.controls

    def GetNonControlledTemplateParameterIds(self):
        return []

    def GetPrimaryViewId(self):
        return self.parent

    def GetDependentViewIds(self):
        return Items(self.children)

    def GetPlacementOnSheetStatus(self):
        return "NotPlaced"

    def CanViewBeDuplicated(self, mode):
        return mode in ("duplicate", "dependent", "detail")

    def IsValidViewTemplate(self, template):
        return template.Value == 8


def point(x, y=0, z=0):
    return NS(X=x, Y=y, Z=z)


class Line:
    IsBound = True

    def __init__(self):
        self.ends = [point(1), point(2)]

    def GetEndPoint(self, end):
        return self.ends[end]


class DetailLine:
    UniqueId, VersionGuid = "detail-20", "fixed"
    Pinned = False
    Parameters = []
    LineStyle = NS(Id=Id(2), UniqueId="style",
                   GraphicsStyleCategory=NS(Name="Thin", LineColor=NS(Red=0, Green=0, Blue=0),
                                            GetLineWeight=lambda _: 1, GetLinePatternId=lambda _: Id(-1)))

    def __init__(self):
        self.Id, self.GroupId = Id(20), Id(-1)
        self.GeometryCurve = Line()

    def GetTypeId(self):
        return Id(-1)

    def GetDependentElements(self, _):
        return NS(Count=0)

    def GetEntitySchemaGuids(self):
        return NS(Count=0)

    def GetAdjoinedCurveElements(self, _):
        return NS(Count=0)


class Viewport:
    pass


class Collector:
    def __init__(self, document):
        self.document = document

    def OfClass(self, kind):
        return (element for element in self.document.elements.values() if isinstance(element, kind))

    def OwnedByView(self, owner):
        self.owner = owner
        return self

    def WhereElementIsNotElementType(self):
        return iter(self.document.detail.get(self.owner.Value, []))


@pytest.fixture
def runtime(monkeypatch):
    builtins = ("VIEW_SCALE", "VIEW_SCALE_PULLDOWN_IMPERIAL", "VIEW_SCALE_PULLDOWN_METRIC",
                "VIEW_DETAIL_LEVEL", "VIEW_PARTS_VISIBILITY", "VIEW_DISCIPLINE",
                "MODEL_GRAPHICS_STYLE_ANON_DRAFT", "VIEW_MODEL_DISPLAY_MODE",
                "VIEWER_BOUND_FAR_CLIPPING", "VIEW_SHOW_HIDDEN_LINES", "COLOR_SCHEME_LOCATION",
                "PLAN_VIEW_NORTH", "VIEW_BACK_CLIPPING")
    db = NS(View=View, Viewport=Viewport, DetailLine=DetailLine, Line=Line, ElementId=Id,
            GraphicsStyleType=NS(Projection="projection"),
            BuiltInParameter=NS(**{name: -index for index, name in enumerate(builtins, 1)}),
            FilteredElementCollector=Collector, NamingUtils=NS(IsValidName=lambda name: "/" not in name),
            WorksharingUtils=NS(GetCheckoutStatus=lambda doc, identifier: doc.owners[identifier.Value],
                                GetModelUpdatesStatus=lambda _doc, _id: "CurrentWithCentral"),
            ViewDuplicateOption=NS(Duplicate="duplicate", WithDetailing="detail", AsDependent="dependent"))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(
        StartNew=lambda: NS(ElapsedMilliseconds=0))))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    main = runpy.run_path(str(SCRIPTS / "duplicate_views.py"))["main"]
    elements = {8: View(8, "Test template", True), 9: View(9, "North")}
    document = NS(PathName="/fixture.rvt", IsFamilyDocument=False, IsReadOnly=False, IsWorkshared=False,
                  ProjectInformation=NS(UniqueId="project"), elements=elements,
                  GetElement=lambda element_id: elements.get(element_id.Value), detail={9: [DetailLine()]})
    return main, document


def read(runtime, mode="detailing", pattern="{name} - {id}"):
    main, document = runtime
    response = main(document, {"request": {"template_id": "8", "pattern": pattern,
                                          "sources": [{"id": "9", "mode": mode}]}})
    return json.loads(response["snapshot_json"])


def test_query_flows_into_preview_and_detects_live_changes(runtime):
    first = read(runtime)
    plan, display = preview.compute(first)
    assert plan["steps"][0]["name"] == "North - 9"
    assert not display["blocked"]
    assert first["template"]["settings"] == [["-1", "Scale", ["Property", "100"]]]
    _, document = runtime
    document.detail[9][0].GeometryCurve.ends[1] = point(3)
    moved = read(runtime)
    assert moved["sources"]["9"]["details"][0]["version"] == first["sources"]["9"]["details"][0]["version"]
    assert preview.compute(moved)[0]["basis_sha256"] != plan["basis_sha256"]
    document.elements[8].Scale = 50
    changed_template = read(runtime)
    assert preview.compute(changed_template)[0]["basis_sha256"] != preview.compute(moved)[0]["basis_sha256"]


def test_unsupported_content_blocks_detailing_and_unknown_template_controls_stop_read(runtime):
    _, document = runtime
    document.detail[9].append(object())
    assert preview.compute(read(runtime))[0]["blocked"]
    assert not preview.compute(read(runtime, mode="plain"))[0]["blocked"]
    document.elements[8].controls.append(Id(-999))
    with pytest.raises(ValueError, match="live value cannot be verified"):
        read(runtime, mode="plain")


def test_native_prohibited_name_stops_the_read(runtime):
    with pytest.raises(ValueError, match="prohibited Revit characters"):
        read(runtime, pattern="{name}/copy")



def test_dependent_template_change_checks_all_existing_family_members(runtime):
    main, document = runtime
    source = document.elements[9]
    sibling = View(10, "North-east")
    sibling.parent = Id(9)
    source.children = [Id(10)]
    document.elements[10] = sibling
    document.IsWorkshared = True
    document.owners = {9: "NotOwned", 10: "OwnedByOtherUser"}
    request = {"template_id": "8", "pattern": "{name} Copy",
               "sources": [{"id": "9", "mode": "dependent"}], "allow_family_template_change": True}
    snapshot = json.loads(main(document, {"request": request})["snapshot_json"])
    plan, display = preview.compute(snapshot)
    assert plan["blocked"]
    assert display["ownership_ids"] == ["9", "10"]
    assert display["warnings"] == [{"row": "view:1", "text": "Worksharing status blocks view 10.",
                                    "blocking": True}]
    document.owners[10] = "NotOwned"
    snapshot = json.loads(main(document, {"request": request})["snapshot_json"])
    assert preview.compute(snapshot)[0]["family_changes"] == {"9": ["9", "10"]}


class Items(list):
    @property
    def Count(self):
        return len(self)


class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self


class Transaction:
    def __init__(self, document, name):
        self.document, self.name = document, name
        self.status = "uninitialized"

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
        doc = self.document
        if doc.emit_changes:
            args = NS(GetDocument=lambda: doc, GetTransactionNames=lambda: [self.name],
                      GetAddedElementIds=lambda: Items(Id(item) for item in doc.added),
                      GetModifiedElementIds=lambda: Items(Id(item) for item in doc.modified),
                      GetDeletedElementIds=lambda: Items(Id(item) for item in doc.deleted))
            for handler in doc.Application.DocumentChanged.handlers:
                handler(doc.Application, args)
        return self.status


class TransactionGroup(Transaction):
    def Start(self):
        self.original_ids = set(self.document.elements)
        self.original_families = {
            item: (element.ViewTemplateId, list(element.children))
            for item, element in self.document.elements.items() if isinstance(element, View)
        }
        return super().Start()

    def RollBack(self):
        for item in set(self.document.elements) - self.original_ids:
            del self.document.elements[item]
        for item, (template, children) in self.original_families.items():
            view = self.document.elements[item]
            view.ViewTemplateId = template
            view.children = children
        return super().RollBack()

    def Assimilate(self):
        self.status = "committed"
        return self.status


@pytest.fixture
def write_runtime(runtime, monkeypatch):
    main, document = runtime
    db = main.__globals__["DB"]
    db.Transaction = Transaction
    db.TransactionGroup = TransactionGroup
    db.TransactionStatus = NS(Started="started", Pending="pending", Committed="committed", RolledBack="rolled_back")
    document.Application = NS(DocumentChanged=Event())
    document.Equals = lambda other: other is document
    document.Regenerate = lambda: None
    document.added, document.modified, document.deleted = [], [], []
    document.emit_changes = True

    def duplicate(_mode):
        identifier = 100 + len(document.added)
        document.elements[identifier] = View(identifier, "Native fallback")
        document.added.append(identifier)
        return Id(identifier)

    monkeypatch.setattr(document.elements[9], "Duplicate", duplicate, raising=False)
    return main, document


def apply_request(runtime, plan=None, display=None):
    if plan is None:
        plan, display = preview.compute(read(runtime, mode="plain"))
    main, document = runtime
    return main(document, {"mode": "apply", "plan": plan, "preview": display})


def test_transaction_uses_exact_name_and_reports_created_identity(write_runtime):
    result = apply_request(write_runtime)
    assert result["status"] == "ok"
    receipt = json.loads(result["receipt_json"])
    assert receipt["views"] == [{"id": "100", "uid": "view-100", "name": "North - 9",
                                 "view_type": "FloorPlan", "primary_id": "-1", "template_id": "8",
                                 "mode": "plain"}]
    assert receipt["changes"] == {"added": ["100"], "modified": [], "deleted": []}
    assert write_runtime[1].Application.DocumentChanged.handlers == []


@pytest.mark.parametrize("failure", ["unapproved_modification", "deleted", "overflow", "missing_event"])
def test_change_group_rolls_back_after_unexpected_transaction_effects(write_runtime, failure):
    _, document = write_runtime
    if failure == "missing_event":
        document.emit_changes = False
    elif failure == "unapproved_modification":
        document.modified = [9]
    elif failure == "deleted":
        document.deleted = [200]
    else:
        document.added = list(range(1000, 3001))
    assert apply_request(write_runtime)["status"] == "stopped"
    assert set(document.elements) == {8, 9}
    assert document.Application.DocumentChanged.handlers == []


def test_apply_requires_unchanged_basis_and_unblocked_linked_preview(write_runtime):
    _, document = write_runtime
    plan, display = preview.compute(read(write_runtime, mode="plain"))
    display["warnings"] = [{"blocking": True}]
    with pytest.raises(ValueError, match="blocking warning"):
        apply_request(write_runtime, plan, display)
    display["warnings"] = []
    document.elements[8].Scale = 50
    with pytest.raises(ValueError, match="model inputs changed"):
        apply_request(write_runtime, plan, display)
    assert set(document.elements) == {8, 9}



def test_dependent_copy_verifies_primary_and_existing_sibling(write_runtime, monkeypatch):
    _, document = write_runtime
    primary = document.elements[9]
    sibling = View(10, "North-east")
    sibling.parent = Id(9)
    primary.children = [Id(10)]
    document.elements[10] = sibling
    duplicate = primary.Duplicate

    def add_dependent(mode):
        assert mode == "dependent"
        new_id = duplicate(mode)
        document.elements[new_id.Value].parent = Id(9)
        primary.children.append(new_id)
        return new_id

    def propagate_template():
        for view_id in primary.children:
            document.elements[view_id.Value].ViewTemplateId = primary.ViewTemplateId
        document.modified = [9, 10]

    monkeypatch.setattr(primary, "Duplicate", add_dependent)
    document.Regenerate = propagate_template
    snapshot = read(write_runtime, mode="dependent")
    snapshot["request"]["allow_family_template_change"] = True
    plan, display = preview.compute(snapshot)
    result = apply_request(write_runtime, plan, display)
    assert result["status"] == "ok"
    receipt = json.loads(result["receipt_json"])
    assert receipt["views"][0]["primary_id"] == "9"
    assert receipt["families"]["10"]["template_id"] == "8"
    assert receipt["changes"]["modified"] == ["9", "10"]


def rollback_record(mode, run_id, result, changes):
    return {"mode": mode, "run_id": run_id, "result": result, "changes": changes,
            "status": "ok", "decision": "rolled_back", "result_truncated": False, "exit_code": 0}


def test_dry_run_verifies_independent_runtime_telemetry(write_runtime):
    plan, display = preview.compute(read(write_runtime, mode="plain"))
    result = apply_request(write_runtime, plan, display)
    record = rollback_record("dry_run", "view-dry-run", result, {
        "added_count": 1, "added": [{"id": 100}], "modified_count": 0, "deleted_count": 0,
        "by_category": {"Views": 1}, "other_documents": [],
    })
    for fields in ({"mode": "modify"}, {"decision": "committed"}, {"result_truncated": True}):
        with pytest.raises(ValueError, match="complete Revit dry_run"):
            preview.verify_dry_run(plan, {**record, **fields})
    receipt = preview.verify_dry_run(plan, record)
    assert receipt["views"][0]["id"] == "100"
    record["changes"]["added_ids"] = [101]
    with pytest.raises(ValueError, match="conflicting addition lists"):
        preview.verify_dry_run(plan, record)
    del record["changes"]["added_ids"]
    record["changes"]["other_documents"] = [{"title": "Other"}]
    with pytest.raises(ValueError, match="runtime change record"):
        preview.verify_dry_run(plan, record)
    record["changes"]["other_documents"] = []
    record["changes"]["added"] = [{"id": 101}]
    with pytest.raises(ValueError, match="runtime change record"):
        preview.verify_dry_run(plan, record)


def test_independent_verification_detects_a_rename_after_keep(write_runtime, tmp_path):
    main, document = write_runtime
    plan, display = preview.compute(read(write_runtime, mode="plain"))
    kept = apply_request(write_runtime, plan, display)
    inputs = {"mode": "verify", "plan": plan, "receipt": json.loads(kept["receipt_json"])}
    readback = main(document, inputs)
    plan_path = tmp_path / "plan.json"
    record = tmp_path / "verified.json"
    plan_path.write_text(json.dumps(plan))
    query = rollback_record("query", "view-reread", readback,
                            {"added_count": 0, "modified_count": 0, "deleted_count": 0})
    record.write_text(json.dumps({"response": query}))
    dry_receipt = json.loads(kept["receipt_json"])
    dry_receipt["views"][0].update(id="110", uid="provisional")
    dry_receipt["changes"]["added"] = ["110"]
    dry = tmp_path / "dry.json"
    dry.write_text(json.dumps(rollback_record(
        "dry_run", "view-dry-run", {"status": "ok", "receipt_json": json.dumps(dry_receipt)},
        {"added_count": 1, "added": [{"id": 110}], "modified_count": 0,
         "deleted_count": 0, "by_category": {"Views": 1}, "other_documents": []},
    )))
    preview.run_verification("compare", str(plan_path), str(record), str(dry))
    record.write_text(json.dumps({**query, "run_id": "view-dry-run"}))
    with pytest.raises(ValueError, match="distinct read-only"):
        preview.run_verification("compare", str(plan_path), str(record), str(dry))
    record.write_text(json.dumps(query))
    dry_receipt["views"][0]["view_type"] = "Section"
    dry_record = json.loads(dry.read_text())
    dry_record["result"]["receipt_json"] = json.dumps(dry_receipt)
    dry.write_text(json.dumps(dry_record))
    with pytest.raises(ValueError, match="differs from the approved dry run"):
        preview.run_verification("compare", str(plan_path), str(record), str(dry))
    document.elements[100].Name = "Changed after Keep"
    with pytest.raises(ValueError, match="approved name"):
        main(document, inputs)


@pytest.mark.parametrize("marker_case", ["approved", "wrong_target", "unreviewed_host"])
def test_dependent_marker_must_point_to_new_view_in_reviewed_host(write_runtime, monkeypatch, marker_case):
    main, document = write_runtime
    db = main.__globals__["DB"]
    db.ElevationMarker = type("ElevationMarker", (), {})
    db.ReferenceableViewUtils = NS(GetReferencedViewId=lambda doc, identifier: doc.elements[identifier.Value].target)
    primary = document.elements[9]
    primary.ViewTemplateId = Id(8)
    document.elements[12] = View(12, "Marker host")
    duplicate = primary.Duplicate

    def add_dependent(mode):
        new_id = duplicate(mode)
        view = document.elements[new_id.Value]
        view.parent = Id(9)
        view.ViewTemplateId = Id(8)
        primary.children.append(new_id)
        document.elements[101] = NS(Id=Id(101), UniqueId="marker-101", ViewSpecific=True, OwnerViewId=Id(12),
                                    Category=NS(Id=Id(-200)), target=Id(9) if marker_case == "wrong_target" else new_id)
        document.added.append(101)
        return new_id

    monkeypatch.setattr(primary, "Duplicate", add_dependent)
    source = {"id": "9", "mode": "dependent"}
    if marker_case != "unreviewed_host":
        source["marker_host_id"] = "12"
    request = {"template_id": "8", "pattern": "{name} Copy", "sources": [source]}
    snapshot = json.loads(main(document, {"request": request})["snapshot_json"])
    plan, display = preview.compute(snapshot)
    if marker_case != "unreviewed_host":
        assert plan["ownership_ids"] == ["9", "12"]
        assert display["notes"] == ["Review marker host Marker host (view 12) for North Copy."]
        display["notes"].append("Ownership: The model is not workshared.")
    result = apply_request(write_runtime, plan, display)
    if marker_case != "approved":
        assert result["status"] == "stopped"
        assert result["message"].startswith("Unverified addition 101 has owner view 12.")
        assert set(document.elements) == {8, 9, 12}
        return
    assert result["status"] == "ok"
    receipt = json.loads(result["receipt_json"])
    assert receipt["markers"] == [{"id": "101", "uid": "marker-101", "host_id": "12", "view_id": "100",
                                    "category_id": "-200"}]
    assert main(document, {"mode": "verify", "plan": plan, "receipt": receipt})["status"] == "ok"
    document.elements[101].target = Id(9)
    with pytest.raises(ValueError, match="marker differs from the receipt"):
        main(document, {"mode": "verify", "plan": plan, "receipt": receipt})
