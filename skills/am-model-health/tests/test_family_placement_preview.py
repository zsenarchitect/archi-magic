from pathlib import Path
from types import SimpleNamespace
import json
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_library_discovery import manifest_digest
from family_placement_preview import build_preview
from read_family_placement import read_snapshot


def item_id(value):
    return SimpleNamespace(Value=value)


@pytest.fixture
def selection():
    value = {"kind": "family-library-placement-selection", "version": 1, "revit_build": "fixture",
             "family": "Example family", "category": {"id": "-1000"}, "type": "Exact type",
             "source": {"path": "A/Fixture.rfa", "catalog": None}, "placement_type": "OneLevelBased",
             "parameters": [{"name": "Width", "id": "42", "storage": "Double",
                             "data_type": "length", "shared_guid": None}],
             "type_values": {"42": {"state": "value", "value": 2.0}}}
    return dict(value, selection_sha256=manifest_digest(value))


def test_read_reuses_exact_type_and_reports_parameter_change(selection, monkeypatch):
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Int64=int))

    class Level:
        Id = item_id(5)
        UniqueId = "level-uid"
        Name = "Level A"
        ProjectElevation = 20.0

    class Family:
        Id = item_id(18)
        UniqueId = "family-uid"
        FamilyCategory = SimpleNamespace(Id=item_id(-1000))
        FamilyPlacementType = "OneLevelBased"
        Name = "Example family"

        def GetFamilySymbolIds(self):
            return [41, 43]

    class Symbol:
        def __init__(self, identifier, name, family):
            self.Id = item_id(identifier)
            self.UniqueId = "symbol-%s" % identifier
            self.Name = name
            self.Family = family
            self.IsActive = False

        def GetParameters(self, name):
            assert name == "Width"
            return [SimpleNamespace(StorageType="Double", HasValue=True, AsDouble=lambda: 3.0,
                                    Definition=SimpleNamespace(GetDataType=lambda: SimpleNamespace(TypeId="length")))]

    family = Family()
    project = SimpleNamespace(PathName="/fixture/Standalone.rvt", IsFamilyDocument=False, IsReadOnly=False,
                              IsWorkshared=False, IsModelInCloud=False, IsModified=False,
                              ProjectInformation=SimpleNamespace(UniqueId="project-uid"),
                              Application=SimpleNamespace(VersionBuild="fixture", VersionNumber="2026"))
    exact = Symbol(41, "Exact type", family)
    elements = {5: Level(), 41: exact, 43: Symbol(43, "exact type", family)}
    project.GetElement = elements.get
    db = SimpleNamespace(Level=Level, Family=Family, FamilySymbol=Symbol, ElementId=int,
                         FilteredElementCollector=lambda _: SimpleNamespace(OfClass=lambda _: [family]))

    def read():
        return {"result": {"snapshot_json": json.dumps(read_snapshot(
            project, db, {"selection": selection, "level_id": "5"}
        ))}}

    preview = build_preview(selection, read(), [1, 2, 23], 450)
    assert preview["kind"] == "change-preview" and preview["blocked"]
    assert preview["after"][0][3:5] == ["reuse", "Required"]
    assert preview["ownership_ids"] == ["41"]
    assert preview["review"]["symbol"]["uid"] == "symbol-41"
    assert preview["review"]["rotation_deg"] == 90
    assert preview["review"]["level_offset_ft"] == 3
    assert preview["parameter_differences"][0]["project"] == {"state": "value", "value": 3.0}
    assert manifest_digest(preview["review"]) == preview["review_sha256"]

    exact.IsActive = True
    assert build_preview(selection, read(), [1, 2, 23], 0)["ownership_ids"] == []

    exact.Name = "Different type"
    absent = build_preview(selection, read(), [1, 2, 23], 0)
    assert absent["review"]["action"] == "load"
    assert any("already exists" in warning["text"] for warning in absent["warnings"])


def test_rejects_snapshot_for_different_selection(selection):
    read = {"kind": "family-placement-project-snapshot", "version": 1,
            "selection_sha256": "different", "revit_build": selection["revit_build"]}
    with pytest.raises(ValueError, match="does not match"):
        build_preview(selection, read, [1, 2, 23], 0)


@pytest.mark.parametrize(("action", "extra_effect"), [("reuse", False), ("reuse", True), ("load", False)])
def test_trial_rolls_back_after_provisional_effect_check(selection, monkeypatch, action, extra_effect):
    from contextlib import contextmanager
    import copy
    import math
    import probe_family_placement as probe

    loading = action == "load"
    selection["source"]["rfa"] = {"basename": "Fixture.rfa", "sha256": "a" * 64, "bytes": 10}

    selection.update(generation="test-generation")
    selection.pop("selection_sha256")
    selection["selection_sha256"] = manifest_digest(selection)
    symbol_row = {"id": "41", "uid": "symbol-41", "active": False, "placement_type": "OneLevelBased",
                  "parameters": {"42": {"state": "value", "value": 2.0}}}
    baseline = {
        "kind": "family-placement-project-snapshot", "version": 1, "revit_build": "fixture",
        "revit_year": "2026", "selection_sha256": selection["selection_sha256"], "project": {"project_uid": "fixture"},
        "level": {"id": "5", "uid": "level-5", "name": "Level A", "project_elevation": 20},
        "same_name_families": [], "exact_symbols": [] if loading else [symbol_row],
    }
    preview = build_preview(selection, baseline, [1, 2, 23], 90)
    args = {"index": "fixture-index", "disposable_fixture": True, "preview": preview,
            "baseline_audit_token": "a" * 32,
            "preview_sha256": manifest_digest(preview)}
    if loading:
        args["staging"] = {"fixture": True}

    class XYZ:
        def __init__(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z

        def __add__(self, other):
            return XYZ(self.X + other.X, self.Y + other.Y, self.Z + other.Z)

    XYZ.BasisZ = XYZ(0, 0, 1)

    class EventHook(list):
        def __iadd__(self, handler):
            self.append(handler)
            return self

        def __isub__(self, handler):
            self.remove(handler)
            return self

    class Scope:
        def __init__(self, document, name):
            self.state = "Uninitialized"
            self.name = name

        def Start(self):
            self.state = "Started"
            return self.state

        def GetStatus(self):
            return self.state

        def Dispose(self):
            pass

    class Group(Scope):
        def Assimilate(self):
            self.state = "Committed"
            return self.state

        def RollBack(self):
            symbol.IsActive = False
            elements.pop(99, None)
            if loading:
                elements.pop(40, None)
                elements.pop(41, None)
            self.state = "RolledBack"
            return self.state

    class Transaction(Scope):
        def Commit(self):
            self.state = "Committed"
            event = SimpleNamespace(
                Operation="TransactionCommitted", GetDocument=lambda: project,
                GetTransactionNames=lambda: [self.name],
                GetAddedElementIds=lambda: [item_id(key) for key in ([40, 41, 99] if loading else [99])],
                GetDeletedElementIds=lambda: [],
                GetModifiedElementIds=lambda: [] if loading else (
                    [item_id(41), item_id(5)] if extra_effect else [item_id(41)]
                ),
            )
            for handler in project.Application.DocumentChanged:
                handler(None, event)
            return self.state

    level = SimpleNamespace(Id=item_id(5), UniqueId="level-5")
    symbol = SimpleNamespace(Id=item_id(41), UniqueId="symbol-41", IsActive=False)
    symbol.Activate = lambda: setattr(symbol, "IsActive", True)
    family = SimpleNamespace(Id=item_id(40), UniqueId="family-40", Name=selection["family"],
                             FamilyCategory=SimpleNamespace(Id=item_id(-1000)), FamilyPlacementType="OneLevelBased")
    symbol.Name = selection["type"]
    symbol.Family = family
    elements = {5: level} if loading else {5: level, 41: symbol}

    def load(path, name, options):
        assert loading and name == selection["type"] and str(path) == "pinned/Fixture.rfa"
        elements.update({40: family, 41: symbol})
        return True, symbol

    def place(insertion, chosen, base, structural):
        assert (insertion.X, insertion.Y, insertion.Z) == (1, 2, 3)
        instance = SimpleNamespace(Id=item_id(99), UniqueId="instance-99", Host=None,
                                   Symbol=chosen, LevelId=base.Id,
                                   Location=SimpleNamespace(Point=XYZ(1, 2, 23), Rotation=0))
        elements[99] = instance
        return instance

    def rotate(document, identifier, axis, angle):
        assert math.isclose(angle, math.pi / 2)
        elements[identifier.Value].Location.Rotation = angle

    project = SimpleNamespace(IsModifiable=False, Application=SimpleNamespace(DocumentChanged=EventHook()),
                              GetElement=lambda value: elements.get(getattr(value, "Value", value)), Regenerate=lambda: None,
                              Equals=lambda other: other is project,
                              Create=SimpleNamespace(NewFamilyInstance=place), LoadFamilySymbol=load)
    statuses = SimpleNamespace(**{key: key for key in ("Uninitialized", "Started", "Committed", "RolledBack")})
    db = SimpleNamespace(TransactionStatus=statuses, Transaction=Transaction, TransactionGroup=Group,
                         ElementId=int, XYZ=XYZ, LocationPoint=SimpleNamespace,
                         Structure=SimpleNamespace(StructuralType=SimpleNamespace(NonStructural=0)),
                         Line=SimpleNamespace(CreateBound=lambda start, end: (start, end)),
                         ElementTransformUtils=SimpleNamespace(RotateElement=rotate), IFamilyLoadOptions=object)

    @contextmanager
    def verified_stage(receipt, relative):
        assert receipt == args["staging"] and relative == selection["source"]["path"]
        yield "pinned/Fixture.rfa", selection["source"]

    def read_snapshot(*args, **kwargs):
        if loading and 41 not in elements:
            return baseline
        row = dict(symbol_row, active=symbol.IsActive)
        family_rows = []
        if loading:
            row.update(family_id="40", family_uid="family-40")
            family_rows.append({"id": "40", "uid": "family-40", "name": selection["family"],
                                "category_id": "-1000", "placement_type": "OneLevelBased"})
        return dict(baseline, exact_symbols=[row], same_name_families=family_rows)

    monkeypatch.setattr(probe, "select_for_placement", lambda *args: selection)
    monkeypatch.setattr(probe, "read_snapshot", read_snapshot)
    monkeypatch.setattr(probe, "staged_pair", verified_stage)
    monkeypatch.setattr(probe, "parameter_state", lambda *args: {"state": "value", "value": 2.0})
    monkeypatch.setattr(probe, "inventory", lambda *args: {
        str(key): [value.UniqueId, "fingerprint"] for key, value in elements.items()
    })
    result = probe.trial(project, db, args)
    assert result["rollback_verified"] is True
    assert result["status"] == ("stopped" if extra_effect else "trial_rolled_back")
    assert set(elements) == ({5} if loading else {5, 41}) and symbol.IsActive is False
    assert not project.Application.DocumentChanged
    if not extra_effect:
        from verify_family_placement_probe import verify

        assert result["placement"]["xyz_ft"] == [1, 2, 23]

        def record(run_id, mode, result, effects=None, transaction=None):
            changes = {key + "_count": 0 for key in ("added", "deleted", "modified")}
            changes["by_category"] = {}
            if effects:
                for key, ids in effects.items():
                    changes[key] = [{"id": identifier} for identifier in ids]
                    changes[key + "_count"] = len(ids)
                changes["transactions"] = [transaction]
                changes["by_category"] = {"Fixture category": sum(map(len, effects.values()))}
            return {"run_id": run_id, "mode": mode, "status": "ok", "exit_code": 0,
                    "decision": "rolled_back" if effects else "no_changes", "approval": None,
                    "result_truncated": False, "engine": {"requested": "cpython"}, "changes": changes,
                    "blocked": [], "dialogs": [], "failures": [], "warnings": [], "result": result}

        unchanged = {key: value for key, value in probe.inventory(project, db).items()
                     if key not in preview["ownership_ids"]}
        initial = {"status": "ok", "snapshot_json": json.dumps(baseline),
                   "inventory": result["baseline_inventory"], "absent_ids": [],
                   "preserved_inventory": probe.inventory_record(unchanged),
                   "audit_token": args["baseline_audit_token"], "trial_transaction": None}
        original = record("baseline-run", "query", initial)
        trial = record("trial-run", "dry_run", result, result["effects"], result["transaction"])
        fresh = record("fresh-run", "query", dict(initial, absent_ids=result["effects"]["added"],
                                                  audit_token="b" * 32, trial_transaction=result["transaction"]))
        proof = verify(preview, original, trial, fresh)
        assert proof["status"] == "family_placement_rollback_verified"
        assert proof["action"] == action

        bad = copy.deepcopy(fresh)
        bad["result"]["inventory"]["sha256"] = "0" * 64
        with pytest.raises(ValueError, match="restored project"):
            verify(preview, original, trial, bad)
        bad = copy.deepcopy(fresh)
        bad["result"]["absent_ids"] = []
        with pytest.raises(ValueError, match="fresh absence"):
            verify(preview, original, trial, bad)
        bad = dict(trial, run_id=original["run_id"])
        with pytest.raises(ValueError, match="three independent"):
            verify(preview, original, bad, fresh)
        bad = copy.deepcopy(trial)
        bad["changes"]["added"][0]["id"] = "123"
        with pytest.raises(ValueError, match="bridge reports effects"):
            verify(preview, original, bad, fresh)
        bad = dict(trial, approval="auto")
        with pytest.raises(ValueError, match="required status and approval"):
            verify(preview, original, bad, fresh)

        import apply_family_placement as writer
        approvals = []

        def ask(context):
            approvals.append(context)
            if len(approvals) == 2:
                raise ValueError("Policy changed before the bridge handoff.")

        monkeypatch.setattr(writer, "require_ask", ask)
        request = {"placement": args, "before": original, "trial": trial, "after": fresh}
        stopped = writer.apply(project, db, request, "bridge")
        assert stopped["status"] == "stopped"
        assert stopped["rollback_verified"]
        assert set(elements) == ({5} if loading else {5, 41})

        def require_ask(context):
            approvals.append(context)

        monkeypatch.setattr(writer, "require_ask", require_ask)
        elements[123] = SimpleNamespace(UniqueId="changed-project")
        with pytest.raises(ValueError, match="project changed"):
            writer.apply(project, db, request, "bridge")
        assert 99 not in elements
        del elements[123]
        handed_off = writer.apply(project, db, request, "bridge")
        assert handed_off["status"] == "awaiting_bridge_decision"
        assert handed_off["effects"] == result["effects"]
        assert 99 in elements and symbol.IsActive
        assert not project.Application.DocumentChanged

        import read_family_placement as reader
        from verify_family_placement_decision import verify as verify_decision

        monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Int64=int))
        monkeypatch.setattr(reader, "inventory", probe.inventory)
        monkeypatch.setattr(reader, "read_snapshot", read_snapshot)
        db.FamilyInstance = db.Level = SimpleNamespace
        decision = {"kept": True, "transaction": handed_off["transaction"], "instance_id": "99",
                    "added_ids": handed_off["effects"]["added"], "modified_ids": handed_off["effects"]["modified"]}
        read_request = {"selection": selection, "level_id": "5", "decision_audit": decision}
        kept_run = dict(record("modify-run", "modify", handed_off, handed_off["effects"], handed_off["transaction"]),
                        approval="user", decision="committed")
        checked = record("decision-read", "query", reader.read_result(project, db, read_request))
        kept = verify_decision(preview, original, trial, fresh, kept_run, checked)
        assert kept["status"] == "family_placement_keep_verified"
        assert kept["placement"]["uid"] == "instance-99"

        wrong_pose = copy.deepcopy(checked)
        wrong_pose["result"]["pose"]["xyz_ft"][2] += 1
        with pytest.raises(ValueError, match="retained instance"):
            verify_decision(preview, original, trial, fresh, kept_run, wrong_pose)
        drifted_run = copy.deepcopy(kept_run)
        drifted_read = copy.deepcopy(checked)
        tolerance = probe.POSITION_TOLERANCE_FT
        drifted_run["result"]["placement"]["xyz_ft"][2] += 0.75 * tolerance
        drifted_read["result"]["pose"]["xyz_ft"][2] += 1.5 * tolerance
        with pytest.raises(ValueError, match="reviewed location"):
            verify_decision(preview, original, trial, fresh, drifted_run, drifted_read)

        unrelated = copy.deepcopy(checked)
        unrelated["result"]["preserved_inventory"]["sha256"] = "0" * 64
        with pytest.raises(ValueError, match="preserved project scope"):
            verify_decision(preview, original, trial, fresh, kept_run, unrelated)
        with pytest.raises(ValueError, match="required status and approval"):
            verify_decision(preview, original, trial, fresh, dict(kept_run, approval="auto"), checked)

        decision["kept"] = False
        with pytest.raises(ValueError, match="still exists"):
            reader.read_result(project, db, read_request)
        Group(project, "fixture").RollBack()
        discarded_run = dict(kept_run, decision="discarded", status="rejected")
        restored = record("discard-read", "query", reader.read_result(project, db, read_request))
        assert verify_decision(preview, original, trial, fresh, discarded_run, restored)["status"] == (
            "family_placement_discard_verified"
        )




@pytest.mark.parametrize("shared", [False, True])
def test_exact_load_rejects_both_conflict_callbacks(selection, shared):
    from probe_family_placement import load_missing_symbol

    callbacks = []
    db = SimpleNamespace(IFamilyLoadOptions=object, FamilySource=SimpleNamespace(Project="Project"))

    def load(path, name, options):
        assert path == "pinned/Fixture.rfa" and name == "Exact type"
        if shared:
            assert options.OnSharedFamilyFound(None, True, None, True) == (False, "Project", False)
        else:
            assert options.OnFamilyFound(True, True) == (False, False)
        return False, None

    with pytest.raises(ValueError, match="conflicting family load"):
        load_missing_symbol(SimpleNamespace(LoadFamilySymbol=load), db, "pinned/Fixture.rfa", selection, callbacks)
    assert callbacks == ["existing_shared_family" if shared else "existing_family"]
