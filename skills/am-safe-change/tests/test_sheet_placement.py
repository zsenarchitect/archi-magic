"""Check repeat anchors and geometry using measured-sheet fixtures."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sheet_placement_preview as preview


def scope(sheet_id, usable):
    return {"usable_ft": usable, "title_block_id": str(int(sheet_id) + 20),
            "title_block_uid": f"ab-{sheet_id}", "revision_sha256": "b" * 64}


def sheet(sheet_id, number, outline, measured):
    return {**{key: value for key, value in measured.items() if key != "usable_ft"},
            "id": sheet_id, "uid": f"aa-{sheet_id}", "number": number, "outline_ft": outline,
            "ownership_ok": True, "placements": [], "obstacles": []}


def view(view_id, kind):
    return {"id": view_id, "uid": f"ac-{view_id}", "name": kind.title(), "kind": kind,
            "eligible": True, "saved_position": False, "placed_sheet_ids": []}


def snapshot():
    scopes = {"10": scope("10", [0, 0, 4, 3]), "11": scope("11", [0.2, 0.1, 3, 2])}
    return {
        "kind": "sheet-placement-snapshot", "complete": True,
        "document": {"project_uid": "aa-01", "path_sha256": "a" * 64},
        "request": {"clearance_ft": 0.05, "sheets": scopes, "placements": [
            {"view_id": "90", "sheet_ids": ["10", "11"], "point_ft": [1, 1]},
            {"view_id": "91", "sheet_ids": ["10", "11"], "point_ft": [2, 1]},
        ]},
        "views": [view("90", "legend"), view("91", "schedule")],
        "sheets": [sheet("10", "A101", [0, 0, 5, 3.5], scopes["10"]),
                   sheet("11", "A102", [0.1, 0.05, 3.5, 2.5], scopes["11"])],
        "viewport_eligible_pairs": [["10", "90"], ["11", "90"]],
    }


def measured(step, identifier, shapes):
    return {**step, "id": identifier, "uid": f"ad-{identifier}", "rectangles_ft": shapes}


def test_repeat_uses_same_absolute_anchor_in_preview_cli(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot()))
    plan = json.loads(subprocess.check_output([sys.executable, SCRIPTS / "sheet_placement_preview.py", path]))
    assert not plan["blocked"]
    assert [(row["sheet_id"], row["kind"], row["point_ft"]) for row in plan["steps"]] == [
        ("10", "legend", [1, 1]), ("11", "legend", [1, 1]),
        ("10", "schedule", [2, 1]), ("11", "schedule", [2, 1]),
    ]
    assert plan["ownership_ids"] == ["10", "11"]


def test_measured_geometry_requires_label_clearance_and_all_sheets_to_fit():
    data = snapshot()
    data["sheets"][1]["obstacles"] = [{"id": "32", "rectangles_ft": [[1.25, 0.5, 1.40, 0.8]]}]
    plan = preview.compute(data)
    shapes = [[0.8, 0.9, 1.2, 1.15], [0.8, 0.7, 1.2, 0.9]]
    placed = [
        measured(plan["steps"][0], "100", shapes),
        measured(plan["steps"][1], "101", shapes),
        measured(plan["steps"][2], "102", [[1.9, 0.65, 2.6, 1.1]]),
        measured(plan["steps"][3], "103", [[1.9, 0.65, 2.6, 1.1]]),
    ]
    assert preview.verify_layout(plan, data, placed) == placed
    placed[1] = {**placed[1], "rectangles_ft": [shapes[0], [0.8, 0.7, 1.201, 0.9]]}
    with pytest.raises(ValueError, match="overlaps"):
        preview.verify_layout(plan, data, placed)
    placed[1] = {**placed[1], "rectangles_ft": shapes}
    placed[-1] = {**placed[-1], "rectangles_ft": [[1.9, 0.65, 3.01, 1.1]]}
    with pytest.raises(ValueError, match="exceeds"):
        preview.verify_layout(plan, data, placed)


def test_same_pair_at_the_correct_anchor_is_a_noop_and_wrong_anchor_blocks():
    data = snapshot()
    for i, sheet_row in enumerate(data["sheets"]):
        sheet_row["placements"] = [
            {"id": str(101 + 2 * i), "uid": f"ad-{101 + 2*i}", "view_id": "90",
             "kind": "legend", "point_ft": [1, 1], "rectangles_ft": [[0.8, 0.9, 1.2, 1.1]]},
            {"id": str(102 + 2 * i), "uid": f"ad-{102 + 2*i}", "view_id": "91",
             "kind": "schedule", "point_ft": [2, 1], "rectangles_ft": [[1.9, 0.65, 2.6, 1.1]]},
        ]
    for item in data["views"]:
        item["placed_sheet_ids"] = ["10", "11"]
    data["viewport_eligible_pairs"] = []
    plan = preview.compute(data)
    assert plan["steps"] == [] and not plan["blocked"] and plan["ownership_ids"] == []
    assert all(row[-1] == "Already placed" for row in plan["after"])
    legend = data["sheets"][1]["placements"][0]
    original = legend["rectangles_ft"]
    for label_bounds, reason in (([0.8, 0.01, 1.2, 0.09], "exceeds the reviewed sheet area"),
                                 ([1.8, 0.8, 1.95, 1.0], "overlaps another sheet object")):
        legend["rectangles_ft"] = original + [label_bounds]
        repeated = preview.compute(data)
        assert repeated["blocked"] and repeated["steps"] == []
        assert any(warning["row"] == "2" and reason in warning["text"] for warning in repeated["warnings"])
    legend["rectangles_ft"] = original
    data["sheets"][1]["placements"][0]["point_ft"] = [1.01, 1]
    plan = preview.compute(data)
    assert plan["blocked"] and plan["after"][1][4] == [1.01, 1] and plan["steps"] == []


@pytest.mark.parametrize("mutation", ["saved_position", "ordinary_repeat", "other_sheet", "ownership"])
def test_blocks_unsupported_or_unowned_placements(mutation):
    data = snapshot()
    if mutation == "saved_position":
        data["views"][0]["saved_position"] = True
    elif mutation in ("ordinary_repeat", "other_sheet"):
        data["views"][0]["kind"] = "view"
        if mutation == "other_sheet":
            data["views"][0]["placed_sheet_ids"] = ["14"]
            data["request"]["placements"][0]["sheet_ids"] = ["10"]
            data["viewport_eligible_pairs"] = [["10", "90"]]
    else:
        data["sheets"][1]["ownership_ok"] = False
    result = preview.compute(data)
    assert result["blocked"] and result["steps"] == [] and result["ownership_ids"] == []


def test_layout_stops_when_reviewed_sheet_revision_state_has_changed():
    data = snapshot()
    data["sheets"][1]["revision_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="revision state"):
        preview.compute(data)


@pytest.fixture
def placement_host(monkeypatch):
    import runpy
    from types import ModuleType, SimpleNamespace as Record

    def xyz(x, y, z=0):
        return Record(X=x, Y=y, Z=z)

    def identifier(value):
        return Record(Value=int(value))

    def element(id, **values):
        return {"Id": identifier(id), "UniqueId": f"ab-{id}", **values}

    def outline(low, high, *, empty=False):
        return Record(MinimumPoint=xyz(*low), MaximumPoint=xyz(*high),
                      IsEmpty=empty, Dispose=lambda: None)

    class View(Record):
        pass

    class Sheet(View):
        def GetAllRevisionIds(self):
            return IdList()

        def GetAllViewports(self):
            return IdList(item.Id for item in document.items.values()
                          if isinstance(item, Viewport) and item.SheetId.Value == self.Id.Value)

        @property
        def Outline(self):
            return Record(Min=Record(U=0, V=0), Max=Record(U=self.width, V=3), Dispose=lambda: None)

    class Schedule(View):
        def GetSegmentCount(self):
            return 1

        def GetScheduleInstances(self, segment):
            assert segment == -1
            return IdList(item.Id for item in document.items.values()
                          if isinstance(item, ScheduleInstance) and item.ScheduleId.Value == self.Id.Value)

    class Block(Record):
        def GetTypeId(self):
            return identifier(60)

    class Symbol(Record):
        pass

    class Viewport(Record):
        @staticmethod
        def CanAddViewToSheet(doc, sheet, view):
            return not any(isinstance(item, Viewport) and item.ViewId.Value == view.Value
                           and item.SheetId.Value == sheet.Value for item in doc.items.values())

        @staticmethod
        def Create(doc, sheet, view, point):
            item = Viewport(**element(doc.next_id, ViewId=view, OwnerViewId=sheet, SheetId=sheet,
                                      center=xyz(point.X + 0.2, point.Y)))
            doc.items[doc.next_id] = item
            doc.next_id += 1
            return item

        def SetBoxCenter(self, point):
            self.center = point

        def GetBoxCenter(self):
            return self.center

        def GetBoxOutline(self):
            point = self.center
            return outline((point.X - 0.2, point.Y - 0.1), (point.X + 0.2, point.Y + 0.1))

        def GetLabelOutline(self):
            point = self.center
            return outline((point.X - 0.2, point.Y - 0.3), (point.X + 0.2, point.Y - 0.1))

    class ScheduleInstance(Record):
        @staticmethod
        def Create(doc, sheet, view, point):
            item = ScheduleInstance(**element(doc.next_id, OwnerViewId=sheet, ScheduleId=view, Point=point,
                                               IsTitleblockRevisionSchedule=False, SegmentIndex=-1))
            doc.items[doc.next_id] = item
            doc.next_id += 1
            return item

        def get_BoundingBox(self, sheet):
            # The local box is not in sheet coordinates. Transform every corner.
            right = 0.6 if document.geometry_drift else 0.5
            if document.overflow and sheet.Id.Value == 11:
                right = 1.2
            transform = Record(OfPoint=lambda point: xyz(self.Point.X + point.X, self.Point.Y + point.Y))
            return Record(Min=xyz(0, -0.2), Max=xyz(right, 0.1), Transform=transform, Dispose=lambda: None)

    class IdList(list):
        @property
        def Count(self):
            return len(self)

    class Collector:
        def __init__(self, doc):
            self.items = list(doc.items.values())

        def OfClass(self, cls):
            self.items = [item for item in self.items if isinstance(item, cls)]
            return self

        def OwnedByView(self, owner):
            self.items = [item for item in self.items if getattr(item, "OwnerViewId", identifier(-1)).Value == owner.Value]
            return self

        def WhereElementIsNotElementType(self):
            return self

        def Dispose(self):
            pass

        def __iter__(self):
            return iter(self.items)

    status = Record(Started="started", Committed="committed", RolledBack="rolled_back", Pending="pending")

    class Event:
        callback = None

        def __iadd__(self, callback):
            self.callback = callback
            return self

        def __isub__(self, callback):
            assert self.callback == callback
            self.callback = None
            return self

    class Group:
        def __init__(self, doc, label):
            self.doc, self.label, self.state = doc, label, "new"

        def Start(self):
            self.doc.before = dict(self.doc.items)
            self.doc.groups += 1
            self.state = status.Started
            return self.state

        def Assimilate(self):
            self.state = status.Committed
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            self.doc.items = self.doc.before
            self.state = status.RolledBack
            return self.state

    class Transaction(Group):
        def Start(self):
            self.state = status.Started
            return self.state

        def Commit(self):
            doc = self.doc
            additions = IdList(item.Id for key, item in doc.items.items() if key not in doc.before)
            modified = IdList([identifier(10)] if doc.modified else [])
            if doc.late_anchor:
                next(item for item in doc.items.values() if isinstance(item, Viewport)).center.X += 0.1
            event = Record(GetDocument=lambda: doc, GetTransactionNames=lambda: [self.label],
                           GetAddedElementIds=lambda: additions, GetModifiedElementIds=lambda: modified,
                           GetDeletedElementIds=lambda: IdList())
            doc.Application.DocumentChanged.callback(None, event)
            self.state = status.Committed
            return self.state

    document = Record(items={}, next_id=100, groups=0, overflow=False, modified=False, late_anchor=False, geometry_drift=False,
                      IsFamilyDocument=False, IsReadOnly=False, IsWorkshared=False, PathName="/tmp/placement.rvt",
                      ProjectInformation=Record(UniqueId="ab-01"), Application=Record(DocumentChanged=Event()))
    document.GetElement = lambda key: document.items.get(key.Value)
    document.Equals = lambda other: document is other
    document.Regenerate = lambda: None
    document.items[60] = Symbol(**element(60, VersionGuid="sheet-type-1"))
    for id, width in ((10, 4), (11, 3)):
        document.items[id] = Sheet(**element(id, width=width, SheetNumber=f"A{id}", IsPlaceholder=False))
        document.items[id + 20] = Block(**element(id + 20, OwnerViewId=identifier(id), VersionGuid="fixture"))
    document.items[90] = View(**element(90, IsTemplate=False, ViewType="legend", Name="Legend",
                                        ViewPositionId=identifier(-1)))
    document.items[91] = Schedule(**element(91, IsTemplate=False, Name="Schedule", ViewPositionId=identifier(-1),
                                            IsTitleblockRevisionSchedule=False, IsInternalKeynoteSchedule=False,
                                            Definition=Record(IsFilteredBySheet=False)))
    db = Record(View=View, ViewSheet=Sheet, ViewSchedule=Schedule, ViewType=Record(Legend="legend"),
                FamilySymbol=Symbol,
                Viewport=Viewport, ScheduleSheetInstance=ScheduleInstance, FilteredElementCollector=Collector,
                Transaction=Transaction, TransactionGroup=Group, TransactionStatus=status, XYZ=xyz, Revision=Record)
    api = ModuleType("pyrevit.api")
    api.DB = db
    monkeypatch.setitem(sys.modules, "pyrevit.api", api)
    helpers = ModuleType("create_sheets")
    helpers.document_identity = lambda doc: {"project_uid": doc.ProjectInformation.UniqueId, "path_sha256": "a" * 64}
    helpers.element_id = identifier
    helpers.element_key = lambda key: str(key.Value)
    helpers.owned_title_block = lambda doc, sheet: doc.items[sheet.Id.Value + 20]
    monkeypatch.setitem(sys.modules, "create_sheets", helpers)
    system, diagnostics = ModuleType("System"), ModuleType("System.Diagnostics")
    system.Int64, diagnostics.Stopwatch = int, object
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setitem(sys.modules, "System.Diagnostics", diagnostics)
    duplicates = ModuleType("duplicate_views")
    for name, function in runpy.run_path(str(SCRIPTS / "duplicate_views.py")).items():
        setattr(duplicates, name, function)
    monkeypatch.setitem(sys.modules, "duplicate_views", duplicates)
    script = runpy.run_path(str(SCRIPTS / "place_sheet_views.py"))
    main = script["main"]
    catalog = json.loads(main(document, {"mode": "sheet-catalog", "sheet_ids": ["10", "11"]})["catalog_json"])
    request = snapshot()["request"]
    for row in catalog["sheets"]:
        request["sheets"][row["id"]].update({key: row[key] for key in
                                            ("title_block_id", "title_block_uid", "revision_sha256")})
    return document, main, request


def test_native_read_apply_verify_and_noop_uses_one_group(placement_host):
    doc, main, request = placement_host
    baseline = json.loads(main(doc, {"request": request})["snapshot_json"])
    plan = preview.compute(baseline)
    outcome = main(doc, {"mode": "dry-run", "plan": plan})
    assert outcome["status"] == "ok", outcome
    receipt = json.loads(outcome["receipt_json"])
    assert [item["point_ft"] for item in receipt["placements"]] == [[1, 1], [1, 1], [2, 1], [2, 1]]
    assert receipt["placements"][-1]["rectangles_ft"] == [[2, 0.8, 2.5, 1.1]]
    result = main(doc, {"mode": "verify", "plan": plan, "receipt": receipt})
    assert json.loads(result["verification_json"]) == receipt
    replay = preview.compute(json.loads(main(doc, {"request": request})["snapshot_json"]))
    assert all(item[-1] == "Already placed" for item in replay["after"])
    assert main(doc, {"mode": "dry-run", "plan": replay})["status"] == "no_changes"
    assert doc.groups == 1


@pytest.mark.parametrize("failure", ["overflow", "modified", "late_anchor"])
def test_native_placement_rolls_back_the_whole_batch(placement_host, failure):
    doc, main, request = placement_host
    baseline = json.loads(main(doc, {"request": request})["snapshot_json"])
    original = set(doc.items)
    setattr(doc, failure, True)
    outcome = main(doc, {"mode": "dry-run", "plan": preview.compute(baseline)})
    assert outcome["status"] == "stopped", outcome
    assert doc.groups == 1 and set(doc.items) == original
    assert json.loads(main(doc, {"request": request})["snapshot_json"]) == baseline
    assert doc.Application.DocumentChanged.callback is None


def test_title_block_type_change_invalidates_measured_usable_area(placement_host):
    doc, main, request = placement_host
    baseline = json.loads(main(doc, {"request": request})["snapshot_json"])
    plan = preview.compute(baseline)
    doc.items[60].VersionGuid = "sheet-type-changed"
    with pytest.raises(ValueError, match="title block or revision state"):
        main(doc, {"mode": "dry-run", "plan": plan})
    assert doc.groups == 0


def test_runtime_receipts_allow_new_ids_but_reject_duplicate_runs_and_changed_geometry(placement_host):
    doc, main, request = placement_host
    baseline = json.loads(main(doc, {"request": request})["snapshot_json"])
    plan = preview.compute(baseline)
    receipt = json.loads(main(doc, {"mode": "dry-run", "plan": plan})["receipt_json"])
    dry_receipt = {**receipt, "placements": [
        {**item, "id": str(200 + index), "uid": f"ae-{200 + index}"}
        for index, item in enumerate(receipt["placements"])
    ]}

    def record(id, decision, key, data, added=()):
        return {"run_id": id, "decision": decision, "status": "ok", "approval": "user",
                "result": {"status": "ok", key: preview.encode(data)},
                "changes": {"added_ids": list(added), "added_count": len(added), "modified_count": 0,
                            "deleted_count": 0, "by_category": {"Placements": len(added)}}}

    dry = record("dry", "rolled_back", "receipt_json", dry_receipt, preview.receipt_ids(plan, dry_receipt))
    kept = record("kept", "committed", "receipt_json", receipt, preview.receipt_ids(plan, receipt))
    fresh = record("fresh", "rolled_back", "verification_json", receipt)
    assert preview.verify_outcome(plan, kept, fresh, "committed", dry) == receipt
    with pytest.raises(ValueError, match="distinct"):
        preview.verify_outcome(plan, kept, {**fresh, "run_id": "kept"}, "committed", dry)
    dry_receipt["placements"][0]["rectangles_ft"][0][2] += 0.01
    changed = record("dry", "rolled_back", "receipt_json", dry_receipt, preview.receipt_ids(plan, dry_receipt))
    with pytest.raises(ValueError, match="differs from the dry run"):
        preview.verify_outcome(plan, kept, fresh, "committed", changed)


def test_real_placement_rolls_back_if_geometry_differs_from_approved_dry_run(placement_host):
    doc, main, request = placement_host
    original = dict(doc.items)
    baseline = json.loads(main(doc, {"request": request})["snapshot_json"])
    plan = preview.compute(baseline)
    dry_result = main(doc, {"mode": "dry-run", "plan": plan})
    assert dry_result["status"] == "ok"
    receipt = json.loads(dry_result["receipt_json"])
    ids = preview.receipt_ids(plan, receipt)
    dry = {"status": "ok", "run_id": "dry-1", "decision": "rolled_back", "result": dry_result,
           "changes": {"added_ids": ids, "added_count": len(ids), "modified_count": 0,
                       "deleted_count": 0, "by_category": {"Placements": len(ids)}}}
    # Simulate the host rolling back its outer dry-run transaction.
    doc.items = dict(original)
    with pytest.raises(ValueError, match="approved dry-run"):
        main(doc, {"mode": "apply", "plan": plan})
    assert doc.groups == 1
    doc.geometry_drift = True
    real = main(doc, {"mode": "apply", "plan": plan, "approved_dry_run": dry})
    assert real["status"] == "stopped" and "approved dry run" in real["message"]
    assert doc.items == original

    doc.geometry_drift = False
    real = main(doc, {"mode": "apply", "plan": plan, "approved_dry_run": dry})
    kept = json.loads(real["receipt_json"])
    assert real["status"] == "ok"
    assert preview.receipt_ids(plan, kept) != ids
    assert preview.same_placement_batch(kept["placements"], receipt["placements"])
