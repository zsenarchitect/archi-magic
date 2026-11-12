import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_library_discovery import manifest_digest
from family_parameter_transfer_preview import preview


def field(identifier, name, formula=None, **changes):
    return {"id": identifier, "name": name, "formula": formula, "formula_driven": formula is not None,
            "data_type": "length", "group": "geometry", "storage": "Double", "instance": False,
            "built_in": None, "shared_guid": None, "reporting": False, "read_only": False,
            "can_assign_formula": True, "yes_no": False, "associations": [], **changes}


def read(role, fields, types):
    record = {"kind": "family-parameter-transfer-read", "version": 1, "role": role, "revit_build": "fixture",
              "category_id": "-1000", "family": "Fixture", "state": {
                  "definitions": {row["id"]: row for row in fields},
                  "types": {name: {key: {"state": "value", "value": value} for key, value in values.items()}
                            for name, values in types.items()},
                  "current_type": next(iter(types)),
                  "reference_scope": "family_document", "coverage": {"complete": True, "issues": []},
                  "dimensions": [],
              }}
    if role == "target":
        record.update(project={"project_uid": "fixture"}, loaded_family={"id": "20", "uid": "family-20"},
                      project_values={"types": [], "instances": []}, nested_families=[])
    return record


@pytest.fixture
def records():
    source = read("source", [
        field("1", "Base Width"), field("2", "Panel Width", "Base Width * 2"),
        field("3", "Total Width", "Panel Width + 1"),
    ], {"Narrow": {"1": 2.0, "2": 4.0, "3": 5.0}, "Wide": {"1": 3.0, "2": 6.0, "3": 7.0}})
    target = read("target", [field("10", "Base Width")],
                  {"Target A": {"10": 2.0}, "Target B": {"10": 3.0}})
    request = {"selected": ["Total Width", "Panel Width"], "type_map": {"Target A": "Narrow", "Target B": "Wide"}}
    return source, target, request


def seal(records):
    source, target, request = records
    source = dict(source, read_sha256=manifest_digest(source))
    target = dict(target, read_sha256=manifest_digest(target))
    request = dict(request, source_sha256=source["read_sha256"], target_sha256=target["read_sha256"])
    return source, target, request


def test_preview_cli_binds_exact_reads_and_orders_additions(records, tmp_path):
    source, target, request = seal(records)
    paths = [tmp_path / name for name in ("source.json", "target.json", "request.json")]
    wrapper = {"response": {"result": {"status": "ok", "snapshot_json": json.dumps(target)}}}
    for path, data in zip(paths, (source, wrapper, request)):
        path.write_text(json.dumps(data))
    run = subprocess.run([sys.executable, str(SCRIPTS / "family_parameter_transfer_preview.py"),
                          "--source", str(paths[0]), "--target", str(paths[1]), "--request", str(paths[2])],
                         capture_output=True, text=True, check=True)
    result = json.loads(run.stdout)
    assert result["target_sha256"] == target["read_sha256"]
    assert result["source_sha256"] == source["read_sha256"]
    assert result["formula_order"] == ["Panel Width", "Total Width"]
    assert result["operations"]["Base Width"]["action"] == "retain"
    assert result["operations"]["Base Width"]["target_id"] == "10"
    assert result["operations"]["Total Width"]["expected_values"] == {
        "Target A": {"state": "value", "value": 5.0}, "Target B": {"state": "value", "value": 7.0}}
    assert result.pop("preview_sha256") == manifest_digest(result)


def test_existing_equivalent_selection_produces_zero_additions(records):
    source, target, request = records
    for key in ("2", "3"):
        new_key = str(10 + int(key))
        target["state"]["definitions"][new_key] = dict(source["state"]["definitions"][key], id=new_key)
        for target_type, source_type in request["type_map"].items():
            target["state"]["types"][target_type][new_key] = source["state"]["types"][source_type][key]
    result = preview(*seal(records))
    assert result["formula_order"] == []
    assert {name: row["action"] for name, row in result["operations"].items()} == {
        "Base Width": "retain", "Panel Width": "retain", "Total Width": "retain",
    }


def test_blocks_tampered_project_values(records):
    source, target, request = seal(records)
    target["project_values"]["types"].append({"changed": True})
    with pytest.raises(ValueError, match="differs from the reviewed read"):
        preview(source, target, request)


@pytest.mark.parametrize("change, reason", [
    ("partial", "reference inventory is incomplete"),
    ("missing_type", "Map every exact target type"),
    ("missing_dependency", "Select missing dependency"),
    ("case_conflict", "case-conflicting name"),
    ("spec_conflict", "existing target definition conflicts"),
    ("value_conflict", "retained value"),
    ("cycle", "contain a cycle"),
    ("unsupported_value", "unsupported, or invalid mapped value"),
    ("new_shared", "cannot add"),
    ("unset", "unset, unsupported"),
    ("boolean_range", "invalid mapped value"),
])
def test_blocks_ineligible_or_conflicting_transfer(records, change, reason):
    source, target, request = records
    source_state, target_state = source["state"], target["state"]
    if change == "partial":
        target_state["coverage"]["complete"] = False
    elif change == "missing_type":
        del request["type_map"]["Target A"]
    elif change == "missing_dependency":
        request["selected"].remove("Panel Width")
    elif change == "case_conflict":
        target_state["definitions"]["10"]["name"] = "base Width"
    elif change == "spec_conflict":
        target_state["definitions"]["10"]["data_type"] = "area"
    elif change == "value_conflict":
        target_state["types"]["Target A"]["10"]["value"] = 8.0
    elif change == "cycle":
        source_state["definitions"]["2"]["formula"] = "Total Width + 1"
    elif change == "unsupported_value":
        source_state["definitions"]["2"]["storage"] = "ElementId"
    elif change == "new_shared":
        source_state["definitions"]["2"]["shared_guid"] = "fixture"
    elif change == "unset":
        source_state["types"]["Wide"]["2"] = {"state": "unset"}
    elif change == "boolean_range":
        source_state["definitions"]["2"].update(storage="Integer", yes_no=True)
        source_state["types"]["Narrow"]["2"]["value"] = 2
    with pytest.raises(ValueError, match=reason):
        preview(*seal(records))


def test_final_namespace_rejects_a_new_ambiguous_formula_binding(records):
    _, target, _ = records
    target["state"]["definitions"]["14"] = field("14", "Base Width * 2")
    for values in target["state"]["types"].values():
        values["14"] = {"state": "value", "value": 12.0}
    with pytest.raises(ValueError, match="unsupported punctuation"):
        preview(*seal(records))


@pytest.fixture
def trial_states(records):
    from copy import deepcopy

    source, target, request = records
    plan = preview(*seal(records))
    baseline = target["state"]
    changed = deepcopy(baseline)
    added = {"Panel Width": "21", "Total Width": "22"}
    for name, identifier in added.items():
        row = plan["operations"][name]
        field = source["state"]["definitions"][row["source_id"]]
        changed["definitions"][identifier] = dict(field, id=identifier)
        for type_name, values in changed["types"].items():
            values[identifier] = dict(row["expected_values"][type_name])
    return plan, baseline, changed, added


@pytest.mark.parametrize("failure", [None, "label", "association", "existing_value", "new_formula_value"])
def test_family_trial_compares_every_protected_effect(trial_states, failure):
    from family_parameter_transfer_effects import verify_family_effects

    plan, baseline, changed, added = trial_states
    if failure == "label":
        changed["dimensions"] = [{"label_id": "21"}]
    elif failure == "association":
        changed["definitions"]["10"]["associations"] = [{"owner_uid": "changed"}]
    elif failure == "existing_value":
        changed["types"]["Target B"]["10"]["value"] = 4.0
    elif failure == "new_formula_value":
        changed["types"]["Target A"]["22"]["value"] = 0.0
    if failure:
        with pytest.raises(ValueError, match="trial"):
            verify_family_effects(plan, baseline, changed, added)
    else:
        observed = verify_family_effects(plan, baseline, changed, added)
        assert observed["Target A"] == {"Panel Width": {"state": "value", "value": 4.0},
                                        "Total Width": {"state": "value", "value": 5.0}}


@pytest.mark.parametrize("unapproved_element", [False, True])
def test_trial_rolls_back_even_after_a_provisional_commit(trial_states, monkeypatch, unapproved_element):
    from types import SimpleNamespace
    import probe_family_parameter_transfer as trial

    plan, baseline, changed, added = trial_states
    statuses = SimpleNamespace(Uninitialized="initial", Started="started", Committed="committed",
                               RolledBack="rolled_back")
    actions = []

    class Events:
        def __iadd__(self, record):
            self.record = record
            return self

        def __isub__(self, record):
            assert record is self.record
            self.record = None
            return self

    class Scope:
        def __init__(self, document, name):
            self.name = name
            self.status = statuses.Uninitialized

        def Start(self):
            self.status = statuses.Started
            return self.status

        def GetStatus(self):
            return self.status

        def RollBack(self):
            actions.append(self.__class__.__name__)
            self.status = statuses.RolledBack
            return self.status

        def Dispose(self):
            pass

    class Transaction(Scope):
        def Commit(self):
            self.status = statuses.Committed
            application.DocumentChanged.record(None, SimpleNamespace(
                GetDocument=lambda: document, Operation="TransactionCommitted",
                GetTransactionNames=lambda: [self.name], GetAddedElementIds=lambda: [],
                GetDeletedElementIds=lambda: [], GetModifiedElementIds=lambda: [],
            ))
            return self.status

    class Group(Scope):
        pass

    application = SimpleNamespace(DocumentChanged=Events())
    document = SimpleNamespace(Application=application, Equals=lambda other: other is document)
    db = SimpleNamespace(Transaction=Transaction, TransactionGroup=Group, TransactionStatus=statuses)
    elements = {"20": ["owner-20", "version"]}
    observed = dict(elements, **({"88": ["unreviewed-88", "version"]} if unapproved_element else {}))
    monkeypatch.setattr(trial, "add_definitions", lambda *args: added)
    monkeypatch.setattr(trial, "transfer_state", lambda *args, **kwargs: changed)
    monkeypatch.setattr(trial, "inventory", lambda *args: observed)

    result = trial.execute_trial(document, db, plan, baseline, elements, "20")
    assert actions == ["Group"]
    assert application.DocumentChanged.record is None
    if unapproved_element:
        assert result == {"status": "stopped", "reason": "The trial changed elements outside the reviewed family additions."}
    else:
        assert result["status"] == "trial_rolled_back"
        assert result["added_ids"] == added


def test_native_mutation_applies_literal_before_formula_and_restores_current_type(records, monkeypatch):
    from types import SimpleNamespace
    from family_parameter_transfer_effects import add_definitions

    source, target, request = records
    source["state"]["definitions"]["4"] = field("4", "Clearance")
    source["state"]["definitions"]["2"]["formula"] = "Base Width * 2 + Clearance"
    source["state"]["types"]["Narrow"]["4"] = {"state": "value", "value": 0.0}
    source["state"]["types"]["Wide"]["4"] = {"state": "value", "value": 0.0}
    request["selected"].append("Clearance")
    plan = preview(*seal(records))
    baseline = target["state"]
    actions = []

    class Manager:
        Types = [SimpleNamespace(Name=name) for name in baseline["types"]]
        CurrentType = Types[0]

        def AddParameter(self, name, group, spec, instance):
            actions.append(("add", name, group, spec, instance))
            return SimpleNamespace(Definition=SimpleNamespace(Name=name), StorageType="Double",
                                   CanAssignFormula=True, Id=SimpleNamespace(Value=len(actions)))

        def Set(self, parameter, value):
            actions.append(("value", self.CurrentType.Name, parameter.Definition.Name, value))

        def SetFormula(self, parameter, formula):
            actions.append(("formula", parameter.Definition.Name, formula))

    manager = Manager()
    db = SimpleNamespace(ForgeTypeId=lambda value: ("ForgeTypeId", value))
    document = SimpleNamespace(IsFamilyDocument=True, IsModifiable=True, FamilyManager=manager, Regenerate=lambda: None)
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Double=lambda value: ("Double", value)))
    assert add_definitions(document, db, plan, baseline) == {"Clearance": "1", "Panel Width": "2", "Total Width": "3"}
    assert actions == [
        ("add", "Clearance", ("ForgeTypeId", "geometry"), ("ForgeTypeId", "length"), False),
        ("add", "Panel Width", ("ForgeTypeId", "geometry"), ("ForgeTypeId", "length"), False),
        ("add", "Total Width", ("ForgeTypeId", "geometry"), ("ForgeTypeId", "length"), False),
        ("value", "Target A", "Clearance", ("Double", 0.0)),
        ("value", "Target B", "Clearance", ("Double", 0.0)),
        ("formula", "Panel Width", "Base Width * 2 + Clearance"),
        ("formula", "Total Width", "Panel Width + 1"),
    ]
    assert manager.CurrentType.Name == "Target A"


@pytest.fixture
def decision_records(records, trial_states, tmp_path):
    from hashlib import sha256
    from family_library_discovery import directory_info, identity, pinned_root
    from probe_family_parameter_transfer import TRANSACTION_NAME

    plan, state, changed, added = trial_states
    source, target, request = seal(records)
    working = tmp_path / "private-copy"
    working.mkdir()
    data = b"private family"
    (working / "Fixture.rfa").write_bytes(data)
    with pinned_root(working) as directory:
        working_copy = {"workspace": str(working), "workspace_identity": identity(directory_info(directory)),
                        "file": {"basename": "Fixture.rfa", "bytes": len(data), "sha256": sha256(data).hexdigest()}}
    transfer = {"source_read": source, "target_read": target, "request": request, "working_copy": working_copy,
                "preview_sha256": plan["preview_sha256"]}
    baseline = {"preview_sha256": plan["preview_sha256"], "copy_sha256": manifest_digest(working_copy), "owner": "20",
                "inventory": {"20": ["owner", "original"]}, "state": state, "kept": False}

    def envelope(mode, run_id, decision, result, effects=None, approval=None, status="ok"):
        effects = effects or {"added": [], "modified": [], "deleted": []}
        changes = {key: [{"id": value} for value in values] for key, values in effects.items()}
        counts = {key + "_count": len(values) for key, values in effects.items()}
        total = sum(counts.values())
        return {"mode": mode, "run_id": str(run_id), "status": status, "decision": decision, "approval": approval,
                "exit_code": 0, "result_truncated": False, "error": None, "engine": {"requested": "cpython"},
                "failures": [], "dialogs": [], "blocked": [], "warnings": [], "result": result,
                "changes": {**changes, **counts, "by_category": {"fixture": total} if total else {},
                            "transactions": [result["transaction"]] if total else []}}

    def query(run_id, snapshot, transaction=None):
        return envelope("query", run_id, "no_changes", {"status": "ok", "audit_token": str(run_id) * 32,
                                                       "snapshot_json": json.dumps(snapshot),
                                                       "trial_transaction": transaction})

    first_name = TRANSACTION_NAME + " " + "a" * 32
    second_name = TRANSACTION_NAME + " " + "b" * 32
    event = {"added": ["21"], "modified": ["20"], "deleted": []}
    values = {type_name: {name: row["expected_values"][type_name] for name, row in plan["operations"].items()
                          if row["action"] == "add"} for type_name in state["types"]}
    result = {"status": "trial_rolled_back", "transaction": first_name, "audit_sha256": manifest_digest(baseline),
              "added_ids": added, "values": values, "effects": [event], "rollback_verified": True,
              "preview_sha256": plan["preview_sha256"]}
    before = query(1, baseline)
    trial = envelope("dry_run", 2, "rolled_back", result, event)
    after = query(3, baseline, first_name)
    replay = dict(result, status="awaiting_bridge_decision", transaction=second_name)
    keep = envelope("modify", 4, "committed", replay, event, approval="user")
    discard = envelope("modify", 4, "discarded", replay, event, approval="user", status="rejected")
    kept_state = dict(baseline, state=changed, kept=True,
                      inventory={"20": ["owner", "kept"], "21": ["definition-21", "new"]})
    return (transfer, before, trial, after), (keep, query(5, kept_state, second_name)), (
        discard, query(5, baseline, second_name))


@pytest.mark.parametrize("kept", [True, False])
def test_transfer_decision_requires_independent_readback(decision_records, kept):
    from verify_family_parameter_transfer import verify_decision

    trial_records, keep, discard = decision_records
    receipt = verify_decision(*trial_records, *(keep if kept else discard))
    assert receipt["status"] == ("family_transfer_kept" if kept else "family_transfer_discarded")
    assert receipt["run_ids"] == ["1", "2", "3", "4", "5"]
    assert receipt.pop("decision_sha256") == manifest_digest(receipt)


@pytest.mark.parametrize("corruption", ["same_run", "changed_baseline", "missing_approval", "changed_kept_value"])
def test_transfer_decision_rejects_invalid_evidence(decision_records, corruption):
    from verify_family_parameter_transfer import verify_decision

    trial_records, (keep, fresh), _ = decision_records
    if corruption == "same_run":
        fresh["run_id"] = keep["run_id"]
    elif corruption == "changed_baseline":
        result = trial_records[-1]["result"]
        original = json.loads(result["snapshot_json"])
        original["inventory"]["20"][1] = "changed"
        result["snapshot_json"] = json.dumps(original)
    elif corruption == "missing_approval":
        keep["approval"] = None
    elif corruption == "changed_kept_value":
        current = json.loads(fresh["result"]["snapshot_json"])
        current["state"]["types"]["Target A"]["21"]["value"] = 99.0
        fresh["result"]["snapshot_json"] = json.dumps(current)
    with pytest.raises(ValueError):
        verify_decision(*trial_records, keep, fresh)


def test_replay_must_match_values_and_logical_effects_before_handoff(decision_records, monkeypatch):
    from apply_family_parameter_transfer import apply

    (transfer, before, trial, after), _, _ = decision_records
    requested = {"transfer": transfer, "before": before, "trial": trial, "after": after}
    actions = []

    def require_ask(context):
        actions.append("ask checked")

    def replay(ui, document, DB, inputs, *, handoff, expected_audit_sha256):
        assert inputs is transfer
        values = trial["result"]["values"]
        added_ids = {"Panel Width": "43", "Total Width": "44"}
        events = [{"added": ["43"], "modified": ["20"], "deleted": []}]
        with pytest.raises(ValueError, match="differs"):
            handoff({}, events, added_ids, expected_audit_sha256)
        with pytest.raises(ValueError, match="differs"):
            handoff(values, [dict(events[0], added=["44"])], added_ids, expected_audit_sha256)
        handoff(values, events, added_ids, expected_audit_sha256)
        return "verified"

    import apply_family_parameter_transfer as module
    monkeypatch.setattr(module, "require_ask", require_ask)
    monkeypatch.setattr(module, "run_trial", replay)
    assert apply(None, None, None, requested, None) == "verified"
    assert actions == ["ask checked", "ask checked"]


def test_reserve_requires_keep_and_uses_a_separate_private_destination(decision_records, tmp_path):
    from family_parameter_transfer_export import reserve

    trial_records, keep, discard = decision_records
    with pytest.raises(ValueError, match="verified native Keep"):
        reserve(*trial_records, *discard, str(tmp_path))
    with pytest.raises(ValueError, match="outside the reviewed working-copy"):
        reserve(*trial_records, *keep, trial_records[0]["working_copy"]["workspace"])
    reservation = reserve(*trial_records, *keep, str(tmp_path))
    destination = Path(reservation["save_as"])
    assert destination.parent.parent.parent == tmp_path
    assert destination.name == "Fixture.rfa"
    assert not destination.exists()


@pytest.mark.parametrize("changed_state", [False, True])
def test_seal_requires_independent_exact_reopen(decision_records, tmp_path, monkeypatch, changed_state):
    from types import SimpleNamespace
    import family_parameter_transfer_export as export

    trial_records, (modify, fresh), _ = decision_records
    reservation = export.reserve(*trial_records, modify, fresh, str(tmp_path))
    destination = Path(reservation["save_as"])
    destination.write_bytes(b"kept family")
    audit = json.loads(fresh["result"]["snapshot_json"])
    state = audit["state"]
    if changed_state:
        state["definitions"]["22"]["formula"] = "Panel Width + 2"
    calls = []
    document = SimpleNamespace(PathName=str(destination))

    def open_document(path):
        assert path == str(destination)
        app.Documents.append(document)
        calls.append("open")
        return document

    def close(save):
        assert save is False
        app.Documents.remove(document)
        calls.append("close")
        return True

    document.Close = close
    app = SimpleNamespace(Documents=[], OpenDocumentFile=open_document)
    ui = SimpleNamespace(Application=app, ActiveUIDocument=None)
    read = {"family": "Fixture", "category_id": "-1000", "revit_build": "fixture", "state": state}
    monkeypatch.setattr(export, "snapshot", lambda doc, db: read)
    monkeypatch.setattr(export, "nested_families", lambda ui, db, doc: [])
    workspace = reservation["workspace"]
    request_digest = reservation["request_sha256"]
    if changed_state:
        with pytest.raises(ValueError, match="differs from the independently verified kept family"):
            export.reopen(ui, None, workspace, request_digest, "pyRevit fixture")
        assert not (Path(workspace) / export.RECEIPT_FILE).exists()
        assert calls == ["open", "close"]
        return
    receipt = export.reopen(ui, None, workspace, request_digest, "pyRevit fixture")
    assert calls == ["open", "close"]
    assert not app.Documents
    assert receipt["loaded_family"] == {"id": "20", "uid": "family-20"}
    assert receipt["state_sha256"] == manifest_digest(state)
    with export.verified_export(workspace, request_digest, expected_derived_sha256=receipt["derived_sha256"]) as sealed:
        assert sealed == (destination, receipt)
    with pytest.raises(ValueError, match="differs from the reviewed reload input"):
        with export.verified_export(workspace, request_digest, expected_derived_sha256="wrong digest"):
            pytest.fail("Unexpected export was made available")
    with pytest.raises(FileExistsError):
        export.reopen(ui, None, workspace, request_digest, "pyRevit fixture")
    destination.write_bytes(b"replaced family")
    with pytest.raises(ValueError, match="does not match the reservation and current file bytes"):
        with export.verified_export(workspace, request_digest):
            pytest.fail("Replaced export was made available")


@pytest.mark.parametrize("collision", [None, "nested", "top_level", "different_tree", "stale_target"])
def test_project_reload_preview_binds_sealed_export_and_rejects_collisions(records, collision):
    from family_parameter_reload_preview import build_preview

    _, target, _ = records
    child = {"id": "30", "uid": "family-30", "name": "Inset", "category_id": "-1000", "children": []}
    target["nested_families"] = [child]
    target["read_sha256"] = manifest_digest(target)
    receipt = {"kind": "family-transfer-derived-rfa", "version": 1, "target_sha256": target["read_sha256"],
               "rfa": {"bytes": 32}, **{key: target[key] for key in ("project", "loaded_family", "family", "category_id", "revit_build",
                                             "nested_families")}}
    families = {"20": {"name": "Fixture", "uid": "family-20"}}
    if collision == "nested":
        families["99"] = {"name": "INSET", "uid": "existing-99"}
    elif collision == "top_level":
        families["99"] = {"name": "fixture", "uid": "existing-99"}
    elif collision == "different_tree":
        receipt["nested_families"] = []
    elif collision == "stale_target":
        receipt["target_sha256"] = "older target"
    receipt["derived_sha256"] = manifest_digest(receipt)
    baseline = {"kind": "family-transfer-reload-baseline", "version": 1, "project": target["project"],
                "revit_build": target["revit_build"], "families": families, "targets": [target]}
    baseline["baseline_sha256"] = manifest_digest(baseline)
    if collision:
        with pytest.raises(ValueError):
            build_preview(baseline, [receipt])
    else:
        result = build_preview(baseline, [receipt])
        assert result["rows"] == [{"loaded_family": target["loaded_family"], "name": "Fixture", "receipt": receipt}]
        assert result.pop("preview_sha256") == manifest_digest(result)


def test_nested_inventory_opens_and_closes_descendants(monkeypatch):
    from types import SimpleNamespace
    from family_transfer_nested import nested_families

    def identity(identifier, name):
        return SimpleNamespace(Id=SimpleNamespace(Value=identifier), UniqueId=f"uid-{identifier}", Name=name,
                               FamilyCategory=SimpleNamespace(Id=SimpleNamespace(Value=-1000)), IsEditable=True,
                               IsInPlace=False)

    owner, child, grandchild = identity(1, "Top"), identity(2, "Child"), identity(3, "Grandchild")
    actions = []

    class Family:
        pass

    class Symbol:
        pass

    def make_document(family, embedded):
        document = SimpleNamespace(OwnerFamily=family, IsFamilyDocument=True, IsModifiable=False, IsModified=False,
                                   IsWorkshared=False, IsModelInCloud=False, embedded=embedded)

        def edit(item):
            assert item in document.embedded
            opened = nested_documents[item.Id.Value]
            application.Documents.append(opened)
            actions.append(("open", item.Name))
            return opened

        def close(save):
            assert save is False
            application.Documents.remove(document)
            actions.append(("close", family.Name))
            return True

        document.EditFamily = edit
        document.Close = close
        document.Equals = lambda other: other is document
        return document

    parent = make_document(owner, [child])
    nested_documents = {2: make_document(child, [grandchild]), 3: make_document(grandchild, [])}
    application = SimpleNamespace(Documents=[parent])
    ui = SimpleNamespace(Application=application, ActiveUIDocument=None)
    db = SimpleNamespace(Family=Family, FamilySymbol=Symbol, FilteredElementCollector=lambda document: SimpleNamespace(
        OfClass=lambda cls: document.embedded if cls is Family else [
            SimpleNamespace(Family=item) for item in document.embedded]))
    result = nested_families(ui, db, parent)
    assert result[0]["name"] == "Child"
    assert result[0]["children"] == [
        {"id": "3", "uid": "uid-3", "name": "Grandchild", "category_id": "-1000", "children": []}]
    assert actions == [("open", "Child"), ("open", "Grandchild"), ("close", "Grandchild"), ("close", "Child")]
    assert application.Documents == [parent]


@pytest.mark.parametrize("change", [None, "type_override", "instance_override", "new_type_value", "extra_parameter"])
def test_reload_checks_every_existing_override_and_new_type_value(change):
    from copy import deepcopy
    from family_parameter_reload_effects import verify_project_values

    before = {"types": [{"id": "21", "uid": "type-21", "type_id": "21", "type_name": "Target A",
                        "parameters": {"40": {"state": "value", "value": 12.0}}}],
              "instances": [{"id": "31", "parameters": {"41": {"state": "value", "value": 17.0}}}]}
    after = deepcopy(before)
    added = {"owner_id": "21", "owner_uid": "type-21", "name": "Panel Width", "shared_guid": None,
             "built_in": None, "data_type": "length", "storage": "Double", "state": "value", "value": 4.0}
    fields = after["types"][0]["parameters"]
    fields["42"] = added
    additions = {"Panel Width": {"data_type": "length", "storage": "Double",
                                 "type_values": {"Target A": {"state": "value", "value": 4.0}}}}
    if change == "type_override":
        fields["40"]["value"] = 2.0
    elif change == "instance_override":
        after["instances"][0]["parameters"]["41"]["value"] = 3.0
    elif change == "new_type_value":
        added["value"] = 2.0
    elif change == "extra_parameter":
        fields["43"] = dict(added, name="Extra")
    if change:
        with pytest.raises(ValueError):
            verify_project_values(before, after, additions)
    else:
        verify_project_values(before, after, additions)
