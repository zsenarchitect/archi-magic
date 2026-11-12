from copy import deepcopy
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class MockId:
    def __init__(self, value):
        self.Value = int(value)


class Filter:
    def __init__(self, value):
        self.Name = "Filter %d" % value
        self.UniqueId = "filter-%d" % value


class SelectionFilter(Filter):
    def GetElementIds(self):
        return [MockId(502), MockId(501)]


class ParameterFilter(Filter):
    def __init__(self, value):
        super().__init__(value)
        self.numeric = NS(
            GetType=lambda: NS(Name="FilterDoubleRule"),
            GetRuleParameter=lambda: MockId(-15),
            GetEvaluator=lambda: NS(GetType=lambda: NS(FullName="Autodesk.Revit.DB.FilterNumericGreater")),
            RuleValue=4.25, Epsilon=0.01, Dispose=lambda: None,
        )
        self.rule = NS(GetType=lambda: NS(Name="FilterInverseRule"), GetRuleParameter=lambda: MockId(-15),
                       GetInnerRule=lambda: self.numeric, Dispose=lambda: None)
        self.expression = NS(GetType=lambda: NS(Name="ElementParameterFilter"), Inverted=False,
                             GetRules=lambda: [self.rule], Dispose=lambda: None)

    def GetCategories(self):
        return [MockId(-300), MockId(-400)]

    def GetElementFilter(self):
        return self.expression


class FillPattern:
    UniqueId = "pattern-68"

    def __init__(self):
        self.grid = NS(Angle=0.125, Offset=0.5, Shift=0.0, Origin=NS(U=0.25, V=0.0),
                       GetSegments=lambda: [0.3, -0.2, 0.0], Dispose=lambda: None)

    def GetFillPattern(self):
        return NS(Name="Drafting pattern", Target="Drafting", HostOrientation="ToView", IsSolidFill=False,
                  GetFillGrids=lambda: [self.grid], Dispose=lambda: None)


class LinePattern:
    @staticmethod
    def GetSolidPatternId():
        return MockId(-300001)

    @staticmethod
    def GetLinePattern(document, identifier):
        assert identifier.Value == -300009
        return NS(Name="Built-in dashed", GetSegments=lambda: [NS(Type="Dash", Length=0.2),
                                                               NS(Type="Space", Length=0.1)],
                  Dispose=lambda: None)


class View:
    Id = MockId(31)
    Name = "Review"
    UniqueId = "view-31"
    ViewTemplateId = MockId(41)
    IsTemplate = False

    def __init__(self, settings, temporary):
        self.settings = settings
        self.temporary = temporary
        self.primary = MockId(-1)
        self.children = []
        self.controls = []
        self.exclusions = []
        self.placement = "NotPlaced"

    def GetPrimaryViewId(self):
        return self.primary

    def GetDependentViewIds(self):
        return self.children

    def GetTemplateParameterIds(self):
        return self.controls

    def GetNonControlledTemplateParameterIds(self):
        return self.exclusions

    def GetPlacementOnSheetStatus(self):
        return self.placement

    def AreGraphicsOverridesAllowed(self):
        return True

    def IsTemporaryViewPropertiesModeEnabled(self):
        return self.temporary

    def GetOrderedFilters(self):
        return [MockId(20), MockId(10)]

    def GetFilterOverrides(self, item):
        return self.settings[item.Value]

    def GetFilterVisibility(self, item):
        return item.Value == 10

    def GetIsFilterEnabled(self, item):
        return item.Value == 20


class Sheet:
    Id = MockId(200)
    UniqueId = "sheet-200"
    SheetNumber = "A-101"


class Viewport:
    Id = MockId(201)
    UniqueId = "viewport-201"
    ViewId = MockId(43)
    SheetId = MockId(200)


class Collector:
    def __init__(self, document):
        self.document = document

    def OfClass(self, category):
        return [element for element in self.document.elements.values() if isinstance(element, category)]


@pytest.fixture
def reader(monkeypatch):
    db = NS(View=View, ElementId=MockId, FilterElement=Filter, LinePatternElement=LinePattern,
            SelectionFilterElement=SelectionFilter, ParameterFilterElement=ParameterFilter,
            FillPatternElement=FillPattern, FilteredElementCollector=Collector, Viewport=Viewport,
            ViewSheet=Sheet, BuiltInParameter=NS(VIS_GRAPHICS_FILTERS=-500))
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=1))))
    helpers = runpy.run_path(str(SCRIPTS / "duplicate_views.py"))
    monkeypatch.setitem(sys.modules, "duplicate_views", NS(**helpers))
    return runpy.run_path(str(SCRIPTS / "read_view_filter_graphics.py"))


def view_fixture(reader, temporary=False):
    disposed = []

    def make_settings(identifier):
        fields = {name: NS(IsValid=False) for name in reader["COLORS"]}
        fields.update({name: MockId(-1) for name in reader["PATTERNS"]})
        fields.update({name: False for name in reader["SWITCHES"]})
        return NS(**fields, IsValidObject=True, CutLineWeight=-1, ProjectionLineWeight=3,
                  Transparency=0, DetailLevel="Undefined", Dispose=lambda: disposed.append(identifier))

    settings = {value: make_settings(value) for value in (10, 20)}
    settings[20].CutLineColor = NS(IsValid=True, Red=0, Green=0, Blue=0)
    settings[20].CutLinePatternId = MockId(-300001)
    settings[20].SurfaceForegroundPatternId = MockId(68)
    settings[10].ProjectionLinePatternId = MockId(-300009)
    settings[20].IsSurfaceForegroundPatternVisible = True
    view = View(settings, temporary)
    elements = {10: SelectionFilter(10), 20: ParameterFilter(20), 31: view, 68: FillPattern()}
    document = NS(IsFamilyDocument=False, IsReadOnly=False, IsWorkshared=False,
                  PathName="fixture.rvt", elements=elements,
                  ProjectInformation=NS(UniqueId="project-1"),
                  GetElement=lambda identifier: elements.get(identifier.Value))
    return document, disposed


def test_native_inventory_preserves_order_flags_and_graphic_sentinels(reader):
    document, disposed = view_fixture(reader)
    result = reader["main"](document, {"view_ids": ["31"]})
    snapshot = json.loads(result["inventory_json"])
    view = snapshot["views"][0]
    assert view["template_id"] == "41"
    assert [(row["id"], row["visible"], row["enabled"]) for row in view["filters"]] == [
        ("20", False, True), ("10", True, False),
    ]
    assert disposed == [20, 10]
    actual = view["filters"][0]["graphics"]
    assert actual["CutLineColor"] == [0, 0, 0] and actual["ProjectionLineColor"] is None
    assert actual["CutLinePatternId"] == {"kind": "solid_line"}
    assert actual["SurfaceForegroundPatternId"] == {"kind": "element", "id": "68"}
    assert actual["ProjectionLinePatternId"] is None and actual["Transparency"] == 0
    assert actual["IsSurfaceForegroundPatternVisible"] is True


def test_temporary_graphics_are_rejected_before_reading_overrides(reader):
    document, disposed = view_fixture(reader, temporary=True)
    with pytest.raises(ValueError, match="Temporary View Properties"):
        reader["main"](document, {"view_ids": ["31"]})
    assert disposed == []


def test_rules_and_patterns_change_snapshot_without_id_changes(reader):
    document, _ = view_fixture(reader)

    def inventory():
        return json.loads(reader["main"](document, {"view_ids": ["31"]})["inventory_json"])

    first = inventory()
    assert first["filter_definitions"] == {
        "10": {"kind": "selection", "members": ["501", "502"]},
        "20": {"kind": "parameter", "categories": ["-400", "-300"],
               "tree": {"type": "ElementParameterFilter", "inverted": False, "rules": [
                   {"type": "FilterInverseRule", "parameter": "-15", "inner": {
                       "type": "FilterDoubleRule", "parameter": "-15",
                       "evaluator": "Autodesk.Revit.DB.FilterNumericGreater", "value": 4.25, "epsilon": 0.01,
                   }},
               ]}},
    }
    assert first["pattern_definitions"] == {
        "-300009": {"kind": "built_in_line", "uid": None, "name": "Built-in dashed",
                    "segments": [["Dash", 0.2], ["Space", 0.1]]},
        "68": {"kind": "fill", "uid": "pattern-68", "name": "Drafting pattern", "target": "Drafting",
               "orientation": "ToView", "solid": False, "grids": [
                   {"angle": 0.125, "offset": 0.5, "shift": 0.0, "origin": [0.25, 0.0],
                    "segments": [0.3, -0.2, 0.0]},
               ]},
    }

    document.GetElement(MockId(20)).numeric.Epsilon = 0.02
    rule_changed = inventory()
    assert rule_changed["views"] == first["views"]
    assert rule_changed["filter_definitions"] != first["filter_definitions"]
    assert rule_changed["pattern_definitions"] == first["pattern_definitions"]
    document.GetElement(MockId(68)).grid.Offset = 0.75
    pattern_changed = inventory()
    assert pattern_changed["views"] == first["views"]
    assert pattern_changed["pattern_definitions"] != first["pattern_definitions"]


def test_unknown_rules_and_oversize_selection_stop_inventory(reader):
    document, _ = view_fixture(reader)
    document.GetElement(MockId(20)).numeric.GetType = lambda: NS(Name="UnknownNativeRule")
    with pytest.raises(ValueError, match="Unsupported native filter rule"):
        reader["main"](document, {"view_ids": ["31"]})
    document.GetElement(MockId(20)).expression = None
    document.GetElement(MockId(10)).GetElementIds = lambda: map(MockId, range(1001))
    with pytest.raises(ValueError, match="filter member inventory exceeds"):
        reader["main"](document, {"view_ids": ["31"]})


def transfer_fixture(reader):
    document, _ = view_fixture(reader)
    source = document.GetElement(MockId(31))
    source.ViewTemplateId = MockId(-1)
    for value in (41, 42, 43):
        view = View(source.settings, False)
        view.Id, view.Name, view.UniqueId = MockId(value), "View %d" % value, "view-%d" % value
        view.ViewTemplateId = MockId(-1)
        document.elements[value] = view
    template, primary, dependent = (document.elements[value] for value in (41, 42, 43))
    template.IsTemplate = True
    template.controls = [MockId(-500)]
    primary.ViewTemplateId = template.Id
    primary.children = [dependent.Id]
    dependent.primary = primary.Id
    dependent.placement = "CompletelyPlaced"
    document.elements[200], document.elements[201] = Sheet(), Viewport()
    return document


def transfer(reader, document, source, *targets):
    request = {"source_id": source, "target_ids": list(targets)}
    result = reader["main"](document, {"request": request})
    return json.loads(result["inventory_json"])


def test_transfer_resolves_template_consumers_and_dependent_sheet_placement(reader):
    document = transfer_fixture(reader)
    snapshot = transfer(reader, document, "31", "41")
    assert snapshot["scope"] == {
        "source_id": "31", "source_authority_id": "31", "allow_placed": False,
        "targets": [{"requested_id": "41", "recipient_id": "41", "affected_ids": ["41", "42", "43"]}],
    }
    assert snapshot["views"]["43"]["authority_id"] == "41"
    assert snapshot["views"]["43"]["placements"] == [["200", "sheet-200", "A-101", "201", "viewport-201"]]
    assert snapshot["views"]["42"]["filters"] == snapshot["views"]["41"]["filters"]
    assert snapshot["views"]["41"]["writable"] and not snapshot["views"]["31"]["writable"]


def test_transfer_resolves_dependent_primary_and_refuses_overlapping_recipients(reader):
    document = transfer_fixture(reader)
    document.elements[41].exclusions = [MockId(-500)]
    snapshot = transfer(reader, document, "31", "43")
    assert snapshot["scope"]["targets"] == [
        {"requested_id": "43", "recipient_id": "42", "affected_ids": ["42", "43"]},
    ]
    with pytest.raises(ValueError, match="scopes overlap"):
        transfer(reader, document, "31", "42", "43")


def test_transfer_refuses_implicit_template_edits_and_mismatched_effective_graphics(reader):
    document = transfer_fixture(reader)
    with pytest.raises(ValueError, match="filter-controlled by template 41"):
        transfer(reader, document, "31", "43")
    with pytest.raises(ValueError, match="scopes overlap"):
        transfer(reader, document, "43", "41")
    document.elements[43].GetFilterVisibility = lambda _id: True
    with pytest.raises(ValueError, match="do not match its identified authority"):
        transfer(reader, document, "31", "41")


def test_preview_preserves_target_only_graphics_and_requires_sheet_approval(reader, tmp_path):
    import subprocess
    from view_filter_transfer_preview import compute

    document = transfer_fixture(reader)
    document.elements[11] = SelectionFilter(11)
    for view_id in (41, 42, 43):
        view = document.elements[view_id]
        view.settings = dict(view.settings)
        view.settings[11] = view.settings[10]
        view.GetOrderedFilters = lambda: [MockId(10), MockId(11), MockId(20)]
        view.GetFilterVisibility = lambda item: item.Value == 20
        view.GetIsFilterEnabled = lambda item: item.Value == 10
    request = {"source_id": "31", "target_ids": ["41"]}
    result = reader["main"](document, {"request": request})
    snapshot = json.loads(result["inventory_json"])
    blocked, display = compute(snapshot)
    assert blocked["blocked"] and blocked["steps"] == []
    assert display["warnings"] == [
        {"row": "43:20", "text": "View 43 [43] Approve the listed sheet placement before transferring filters.",
         "blocking": True},
    ]
    assert [row[5] for row in display["before"] if row[1] == "View 43"] == [3, 1, 2]
    assert [row[5] for row in display["after"] if row[1] == "View 43"] == [1, 2, 3]
    assert {row[2] for row in display["after"] if row[1] == "View 43"} == {"A-101"}

    request["allow_placed"] = True
    result = reader["main"](document, {"request": request})
    snapshot = json.loads(result["inventory_json"])
    plan, display = compute(snapshot)
    assert not plan["blocked"] and display["review_only"]
    assert plan["basis_sha256"] != blocked["basis_sha256"]
    assert plan["ownership_ids"] == ["41", "42", "43"]
    step = plan["steps"][0]
    assert step["recipient_id"] == "41" and step["affected_ids"] == ["41", "42", "43"]
    assert [row["id"] for row in step["after"]] == ["20", "10", "11"]
    assert step["after"][:2] == snapshot["views"]["31"]["filters"]
    assert step["after"][-1] == step["before"][1]

    record = tmp_path / "runtime.json"
    record.write_text(json.dumps({"result": result}))
    completed = subprocess.run([sys.executable, str(SCRIPTS / "view_filter_transfer_preview.py"), str(record)],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"plan": plan, "preview": display}


def test_exact_filter_repeat_has_no_ownership_or_steps(reader):
    from view_filter_transfer_preview import compute

    document = transfer_fixture(reader)
    plan, display = compute(transfer(reader, document, "31", "41"))
    assert not plan["blocked"] and plan["ownership_ids"] == [] and plan["steps"] == []
    assert all(row[4] == "Already matches" for row in display["after"])


def probe_fixture(reader, monkeypatch, fault=None):
    from copy import copy
    from view_filter_transfer_preview import compute

    monkeypatch.setitem(sys.modules, "read_view_filter_graphics", NS(**reader))
    native = runpy.run_path(str(SCRIPTS / "probe_view_filter_transfer.py"))
    document = transfer_fixture(reader)
    document.elements[11] = SelectionFilter(11)
    target = document.elements[41]
    target.settings = dict(target.settings)
    target.settings[11] = target.settings[10]
    order = [10, 11, 20]
    visible = {10: False, 11: False, 20: True}
    enabled = {10: True, 11: False, 20: False}
    changes = []
    target.GetOrderedFilters = lambda: list(map(MockId, order))
    target.GetFilterVisibility = lambda item: visible[item.Value]
    target.GetIsFilterEnabled = lambda item: enabled[item.Value]
    for view_id in (42, 43):
        view = document.elements[view_id]
        for method in ("GetOrderedFilters", "GetFilterOverrides", "GetFilterVisibility", "GetIsFilterEnabled"):
            setattr(view, method, getattr(target, method))

    def remove(item):
        order.remove(item.Value)
        target.settings.pop(item.Value)
        changes.append(("remove", item.Value))

    def add(item):
        assert item.Value not in order
        order.append(item.Value)
        changes.append(("add", item.Value))

    def overrides(item, settings):
        assert item.Value in order
        if fault == "setter" and item.Value == 10:
            raise ValueError("Injected setter failure.")
        target.settings[item.Value] = settings

    target.RemoveFilter, target.AddFilter, target.SetFilterOverrides = remove, add, overrides
    target.SetFilterVisibility = lambda item, value: visible.__setitem__(item.Value, value)
    target.SetIsFilterEnabled = lambda item, value: enabled.__setitem__(item.Value, value)

    class Handlers:
        callback = None

        def __iadd__(self, callback):
            self.callback = callback
            return self

        def __isub__(self, callback):
            assert self.callback is callback
            self.callback = None
            return self

    class Collection(list):
        @property
        def Count(self):
            return len(self)

    class Group:
        def __init__(self, _document, _name):
            self.status = "Uninitialized"

        def Start(self):
            self.saved = (list(order), dict(target.settings), dict(visible), dict(enabled))
            self.status = "Started"
            return self.status

        def RollBack(self):
            order[:] = self.saved[0]
            for live, baseline in zip((target.settings, visible, enabled), self.saved[1:]):
                live.clear()
                live.update(baseline)
            self.status = "RolledBack"
            return self.status

        def GetStatus(self):
            return self.status

    class Transaction(Group):
        def Commit(self):
            if fault == "pending":
                self.status = "Pending"
                return self.status
            self.status = "Committed"
            modified = [MockId(41), MockId(999)] if fault == "telemetry" else [MockId(41)]
            event_document = NS(Equals=lambda _document: False) if fault == "foreign" else document
            event = NS(GetDocument=lambda: event_document, GetTransactionNames=lambda: [native["TRANSACTION_NAME"]],
                       GetAddedElementIds=lambda: Collection(), GetDeletedElementIds=lambda: Collection(),
                       GetModifiedElementIds=lambda: Collection(modified))
            document.Application.DocumentChanged.callback(None, event)
            return self.status

    db = reader["DB"]
    db.OverrideGraphicSettings = copy
    db.TransactionGroup, db.Transaction = Group, Transaction
    db.TransactionStatus = NS(Started="Started", Pending="Pending", RolledBack="RolledBack", Committed="Committed")
    document.Application = NS(DocumentChanged=Handlers())
    document.Equals = lambda other: document is other
    document.Regenerate = lambda: None
    request = {"source_id": "31", "target_ids": ["41"], "allow_placed": True}
    baseline = native["read_snapshot"](document, request)
    plan, display = compute(baseline)
    args = {"disposable_fixture": True, "plan": plan, "preview": display}
    return native, document, request, baseline, args, changes


@pytest.mark.parametrize("fault,expected", [
    (None, "trial_rolled_back"), ("setter", "stopped_and_rolled_back"),
    ("telemetry", "stopped_and_rolled_back"), ("foreign", "transaction_unresolved"),
    ("pending", "transaction_unresolved"),
])
def test_transfer_probe_qualifies_or_rolls_back_failures(reader, monkeypatch, fault, expected):
    native, document, request, baseline, args, changes = probe_fixture(reader, monkeypatch, fault)
    result = native["probe"](document, args)
    assert result["status"] == expected
    assert changes[:3] == [("remove", 10), ("remove", 11), ("remove", 20)]
    assert document.Application.DocumentChanged.callback is None
    if fault != "pending":
        assert native["read_snapshot"](document, request) == baseline
    if fault is None:
        assert changes[3:] == [("add", 20), ("add", 10), ("add", 11)]
        assert result["changes"] == {"added": [], "modified": ["41"], "deleted": []}
        assert result["remaining_ownership_changes"] == []


def test_transfer_probe_rejects_preview_tampering_before_transaction(reader, monkeypatch):
    native, document, _, _, args, changes = probe_fixture(reader, monkeypatch)
    args["preview"]["after"][0][5] = 99
    with pytest.raises(ValueError, match="no longer matches"):
        native["probe"](document, args)
    assert changes == []


def runtime_record(run_id, mode, result, modified=()):
    from view_filter_transfer_preview import TRANSACTION_NAME

    return {"run_id": run_id, "mode": mode, "status": "ok", "decision": "rolled_back",
            "exit_code": 0, "result_truncated": False, "error": None, "result": result,
            "failures": [], "dialogs": [], "blocked": [], "warnings": [],
            "changes": {"added_count": 0, "deleted_count": 0, "modified_count": len(modified),
                        "added": [], "deleted": [], "modified": [{"id": int(view_id)} for view_id in modified],
                        "transactions": [TRANSACTION_NAME] if modified else [],
                        "by_category": {"Views": len(modified)} if modified else {}}}


@pytest.fixture
def verified_fixture(reader, monkeypatch):
    native, document, request, _, args, _ = probe_fixture(reader, monkeypatch)
    original = reader["main"](document, {"request": request})
    result = native["probe"](document, args)
    assert result["status"] == "trial_rolled_back"
    fresh = reader["main"](document, {"request": request})
    return (args["plan"], runtime_record("a00000000001", "query", original),
            runtime_record("b00000000002", "dry_run", result, result["changes"]["modified"]),
            runtime_record("c00000000003", "query", fresh))


def test_verify_fixture_rollback_through_cli(verified_fixture, tmp_path):
    import subprocess

    plan, original, probe, fresh = verified_fixture
    paths = [tmp_path / (name + ".json") for name in ("original", "plan", "probe", "fresh")]
    for path, record in zip(paths, (original, {"plan": plan}, probe, {"response": fresh})):
        path.write_text(json.dumps(record))
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "view_filter_transfer_preview.py"), str(paths[0]),
         "--verify-rollback", *map(str, paths[1:])],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "status": "verified_fixture_rollback", "basis_sha256": plan["basis_sha256"], "document": plan["document"],
        "original_run_id": original["run_id"], "probe_run_id": probe["run_id"], "fresh_run_id": fresh["run_id"],
        "modified_view_ids": ["41"], "remaining_ownership_changes": [],
    }


@pytest.mark.parametrize("fault,error", [
    ("replayed", "distinct"), ("auto", "clean dry_run rollback"), ("truncated", "missing or invalid metadata"),
    ("wrong_id", "disagrees"), ("short_samples", "disagrees"), ("foreign", "unreviewed additions"),
    ("model_drift", "differs from the original"), ("foreign_borrowing", "unreviewed ownership"),
    ("wrong_trial_graphics", "trial filter stacks"), ("missing_trial_graphics", "trial filter stacks"),
])
def test_verify_fixture_rollback_fails_closed(verified_fixture, fault, error):
    from view_filter_transfer_preview import verify_rollback

    plan, original, probe, fresh = verified_fixture
    if fault == "replayed":
        fresh["run_id"] = original["run_id"]
    elif fault == "auto":
        probe["approval"] = "auto"
    elif fault == "truncated":
        fresh["result_truncated"] = True
    elif fault == "wrong_id":
        probe["changes"]["modified"] = [{"id": 42}]
    elif fault == "short_samples":
        probe["changes"]["modified"] = []
    elif fault == "foreign":
        probe["changes"]["other_documents"] = [{"title": "Other project"}]
    elif fault == "wrong_trial_graphics":
        stacks = deepcopy(probe["result"]["trial_filter_stacks"])
        first = next(iter(stacks.values()))[0]
        first["graphics"]["ProjectionLineColor"] = [250, 1, 50]
        probe["result"]["trial_filter_stacks"] = stacks
    elif fault == "missing_trial_graphics":
        probe["result"].pop("trial_filter_stacks", None)
    elif fault == "model_drift":
        inventory = json.loads(fresh["result"]["inventory_json"])
        inventory["views"]["43"]["filters"][0]["visible"] = not inventory["views"]["43"]["filters"][0]["visible"]
        fresh["result"]["inventory_json"] = json.dumps(inventory)
    else:
        probe["result"]["remaining_ownership_changes"] = [
            {"view_id": "31", "before": None, "after": ["OwnedByCurrentUser", "CurrentWithCentral"]},
        ]
    with pytest.raises(ValueError, match=error):
        verify_rollback(plan, original, probe, fresh)


def test_verify_fixture_reports_remaining_borrowing(verified_fixture):
    from view_filter_transfer_preview import verify_rollback

    plan, original, probe, fresh = verified_fixture
    borrowed = ["OwnedByCurrentUser", "CurrentWithCentral"]
    snapshot = json.loads(fresh["result"]["inventory_json"])
    snapshot["views"]["41"]["ownership"] = borrowed
    fresh["result"]["inventory_json"] = json.dumps(snapshot)
    delta = [{"view_id": "41", "before": None, "after": borrowed}]
    probe["result"]["remaining_ownership_changes"] = delta
    report = verify_rollback(plan, original, probe, fresh)
    assert report["status"] == "ownership_reconciliation_required"
    assert report["remaining_ownership_changes"] == delta


def result_pages(record, cuts):
    payload = json.dumps(record["result"], ensure_ascii=False).encode("utf-16-le")
    total = len(payload) // 2
    metadata = dict(record, result=None, result_truncated=True)
    boundaries = [0] + cuts + [total]
    return [
        dict(metadata, result_page={"offset": start, "length": end - start, "total_length": total,
                                    "text": payload[start * 2:end * 2].decode("utf-16-le", "surrogatepass")})
        for start, end in zip(boundaries, boundaries[1:])
    ]


def test_paged_inventory_and_rollback_through_cli(verified_fixture, tmp_path):
    import subprocess

    plan, original, probe, fresh = verified_fixture
    original_pages = list(reversed(result_pages(original, [11, 103])))
    fresh_pages = [{"response": page} for page in result_pages(fresh, [23, 97])]
    paths = [tmp_path / (name + ".json") for name in ("original", "plan", "probe", "fresh")]
    for path, record in zip(paths, (original_pages, plan, result_pages(probe, [32]), fresh_pages)):
        path.write_text(json.dumps(record))
    script = str(SCRIPTS / "view_filter_transfer_preview.py")
    preview = subprocess.run([sys.executable, script, str(paths[0])], capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["plan"] == plan
    verify = subprocess.run(
        [sys.executable, script, str(paths[0]), "--verify-rollback", *map(str, paths[1:])],
        capture_output=True, text=True,
    )
    assert verify.returncode == 0, verify.stderr
    assert json.loads(verify.stdout)["status"] == "verified_fixture_rollback"


def test_paged_result_joins_surrogate_pair_split_by_bridge():
    from view_filter_transfer_preview import complete_record

    result = {"status": "ok", "fixture": "Sheet \U0001f4d0"}
    raw = json.dumps(result, ensure_ascii=False).encode("utf-16-le")
    surrogate_split = raw.index("\U0001f4d0".encode("utf-16-le")) // 2 + 1
    original = runtime_record("d00000000004", "query", result)
    first, second = result_pages(original, [surrogate_split])
    assert complete_record([second, first]) == original


@pytest.mark.parametrize("fault,error", [
    ("missing", "gap"), ("overlap", "overlap"), ("duplicate", "disagree"),
    ("mixed_run", "disagree"), ("modified_metadata", "disagree"),
    ("wrong_page_length", "disagree"), ("oversize", "size"),
])
def test_incomplete_or_mixed_pyrevit_pages_are_rejected(verified_fixture, fault, error):
    from view_filter_transfer_preview import MAX_RESULT_TEXT, complete_record

    _, original, _, _ = verified_fixture
    pages = result_pages(original, [11, 103])
    if fault == "missing":
        pages.pop(1)
    elif fault == "overlap":
        pages[1]["result_page"]["offset"] = 10
    elif fault == "duplicate":
        pages.append(pages[0])
    elif fault == "mixed_run":
        pages[1]["run_id"] = "d00000000004"
    elif fault == "modified_metadata":
        pages[1]["changes"] = dict(pages[1]["changes"], modified_count=1)
    elif fault == "wrong_page_length":
        pages[0]["result_page"]["length"] = 10
    elif fault == "oversize":
        pages[0]["result_page"]["total_length"] = MAX_RESULT_TEXT + 1
    with pytest.raises(ValueError, match=error):
        complete_record(pages)
