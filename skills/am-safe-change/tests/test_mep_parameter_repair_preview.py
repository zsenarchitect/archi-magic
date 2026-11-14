import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mep_parameter_repair_preview.py"
GUID = "62cba752-ce75-4604-93a8-f58f02909dc7"
LARGE_ID = "9007199254740993"


def system_name_evidence(source):
    system = {"id": "31", "uid": "system"}
    return {
        "kind": "mep-system-name-repair-evidence", "version": 1, "source": source,
        "system": dict(system, name="Old", system_type={"id": "30", "uid": "type"},
                       classification="SupplyAir", roles={"network": [LARGE_ID], "terminals": ["22"],
                                                           "base_equipment": []},
                       members=[
                           {"id": "22", "uid": "terminal", "assignments": [{"connector_id": 1, "system": system}]},
                           {"id": LARGE_ID, "uid": "duct", "assignments": [{"connector_id": 2, "system": system}]},
                       ]),
        "proposed_name": "SUPPLY-01", "name_conflicts": [],
    }



@pytest.fixture
def inputs(tmp_path):
    host = {"project_uid": "fixture", "path_sha256": "a" * 64}
    evidence = {
        "kind": "mep-parameter-repair-evidence", "version": 1, "guid": GUID,
        "source": {"host_identity": host, "document_identity": host,
                   "link_instance_id": None, "link_instance_uid": None},
        "elements": [
            {"id": LARGE_ID, "uid": "blank-instance", "category_id": "-2008000",
             "parameter_id": "42", "storage": "String", "state": "unset",
             "read_only": False, "has_value": False, "value": None},
            {"id": "22", "uid": "filled-instance", "category_id": "-2008000",
             "parameter_id": "42", "storage": "String", "state": "already_filled",
             "read_only": False, "has_value": True, "value": "Keep this"},
        ],
    }
    receipt = {
        "run_id": "0123456789ab", "mode": "query", "status": "ok", "decision": "rolled_back",
        "exit_code": 0, "result_truncated": False, "error": None,
        "failures": [], "dialogs": [], "blocked": [], "warnings": [],
        "changes": {"added": [], "modified": [], "deleted": [], "added_count": 0,
                    "modified_count": 0, "deleted_count": 0, "by_category": {}, "transactions": []},
    }
    query = tmp_path / "query.json"
    fill = tmp_path / "fill.json"

    def run(text="Ventilation", **overrides):
        result = {"status": "ok", "repair_parameters_json": json.dumps(evidence)}
        query.write_text(json.dumps({"response": dict(receipt, result=result, **overrides)}))
        fill.write_text(json.dumps({"text": text}))
        return subprocess.run([sys.executable, SCRIPT, query, fill], capture_output=True, text=True)

    return evidence, run


def test_preview_shows_only_blank_text_as_a_proposed_change(inputs):
    _, run = inputs
    result = run()
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert preview["kind"] == "change-preview"
    assert preview["review_only"] is True and preview["steps"] == []
    assert preview["source_run_id"] == "0123456789ab"
    assert [row[0] for row in preview["after"]] == [LARGE_ID, "22"]
    assert [row[-1] for row in preview["before"]] == [None, "Keep this"]
    assert [row[-1] for row in preview["after"]] == ["Ventilation", "Keep this"]
    assert "already_filled" in preview["before"][1]
    assert preview["basis_sha256"] != json.loads(run("Revised").stdout)["basis_sha256"]


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="The preview renderer needs PowerShell.")
def test_preview_renders_with_exact_revit_ids(inputs, tmp_path):
    _, run = inputs
    result = run()
    assert result.returncode == 0, result.stderr
    preview = tmp_path / "preview.json"
    page = tmp_path / "preview.html"
    preview.write_text(result.stdout)
    renderer = SCRIPT.parents[2] / "am-schedule-view" / "scripts" / "render-table.ps1"
    render = subprocess.run(["pwsh", "-NoProfile", "-File", renderer, "-Json", preview, "-Out", page],
                            capture_output=True, text=True)
    assert render.returncode == 0, render.stderr
    embedded = re.search(r'<script type="application/json" id="data">(.*?)</script>',
                         page.read_text(encoding="utf-8-sig"), flags=re.S)
    assert embedded is not None
    payload = json.loads(embedded[1])
    assert payload["after"][0][0] == LARGE_ID and payload["after"][0][-1] == "Ventilation"
    assert payload["after"][1][-1] == "Keep this"


def test_preview_blocks_ineligible_and_inconsistent_rows(inputs):
    evidence, run = inputs
    evidence["elements"].append({"id": "23", "uid": "missing", "category_id": "-2008000",
                                 "state": "missing_parameter"})
    result = run()
    assert result.returncode == 1
    preview = json.loads(result.stdout)
    assert preview["blocked"] is True
    assert preview["warnings"] == [{"row": "23", "text": "The shared parameter is missing.", "blocking": True}]

    evidence["elements"][0]["value"] = "Do not overwrite this"
    result = run()
    assert result.returncode == 2 and not result.stdout


@pytest.mark.parametrize("override", [{"warnings": ["Unexpected native warning"]}, {"decision": "committed"}])
def test_preview_rejects_unverified_query_receipts(inputs, override):
    _, run = inputs
    result = run(**override)
    assert result.returncode == 2 and not result.stdout
    assert "clean query rollback" in result.stderr


@pytest.mark.parametrize("fault, outcome, rename_method", [
    (None, "mep_text_fill_trial_rolled_back", None),
    ("extra", "stopped_and_rolled_back", None),
    ("setter_failed", "stopped_and_rolled_back", None),
    ("foreign", "transaction_unresolved", None),
    ("pending", "transaction_unresolved", None),
    ("survives", "transaction_unresolved", None),
    (None, "mep_system_rename_trial_rolled_back", "element_name"),
    (None, "mep_system_rename_trial_rolled_back", "built_in_parameter"),
    ("extra", "stopped_and_rolled_back", "element_name"),
    ("setter_failed", "stopped_and_rolled_back", "built_in_parameter"),
    ("survives", "transaction_unresolved", "element_name"),
])
def test_native_repair_trial_measures_effects_and_always_rolls_back(inputs, tmp_path, monkeypatch, fault, outcome,
                                                                     rename_method):
    import runpy
    from types import SimpleNamespace as NS

    from test_probe_mep_route import Collection, Handler, RegenerationFailure

    evidence, run = inputs
    display = json.loads(run().stdout)
    args = {"disposable_fixture": True, "query": json.loads((tmp_path / "query.json").read_text()),
            "fill": json.loads((tmp_path / "fill.json").read_text()), "preview": display}
    if rename_method:
        native_evidence = system_name_evidence(evidence["source"])
        record = args["query"]["response"]
        record["result"] = {"status": "ok", "repair_system_name_json": json.dumps(native_evidence)}
        query = tmp_path / "query.json"
        query.write_text(json.dumps(args["query"]))
        rendered = subprocess.run([sys.executable, SCRIPT, query, "--system-name"], capture_output=True, text=True)
        assert rendered.returncode == 0, rendered.stderr
        args["preview"] = json.loads(rendered.stdout)
        args["rename_method"] = rename_method
        del args["fill"]
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    db = NS(TransactionStatus=NS(Started="Started", Committed="Committed", Pending="Pending",
                                 RolledBack="RolledBack", Uninitialized="Uninitialized"))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=object)))
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.Exceptions", NS(RegenerationFailedException=RegenerationFailure))
    monkeypatch.delitem(sys.modules, "duplicate_views", raising=False)
    module = runpy.run_path(str(SCRIPT.with_name("probe_mep_parameter_fill.py")))
    document = NS(PathName="C:/Fixtures/MEP.rvt", IsModifiable=False,
                  Application=NS(VersionNumber="2026", DocumentChanged=Handler()))
    document.Equals = lambda other: other is document
    calls = []
    original = evidence["elements"]
    current = [dict(row) for row in original]

    def read(_doc, request, _clock):
        selected = {item["id"] for item in request["elements"]}
        return {"guid": GUID, "elements": [dict(row) for row in current if row["id"] in selected]}

    def set_value(value):
        if fault == "setter_failed":
            return False
        current[0].update(value=value, has_value=True, state="already_filled")
        return True

    reader = NS(resolve_source=lambda *_: (document, None, evidence["source"]),
                read_repair_parameters=read, element_id=str, Guid=str)
    document.GetElement = lambda identifier: NS(get_Parameter=lambda guid: NS(Set=set_value))

    system = NS(UniqueId="system", Name="Old")
    if rename_method:
        db.BuiltInParameter = NS(RBS_SYSTEM_NAME_PARAM=99)
        db.StorageType = NS(String="string")

        def set_name(value):
            if fault == "setter_failed":
                return False
            system.Name = value
            return True

        system.get_Parameter = lambda identifier: NS(Element=system, IsReadOnly=False, StorageType="string",
                                                      AsString=lambda: system.Name, Set=set_name)
        document.GetElement = lambda identifier: system

        def read_name(_doc, _request, _clock):
            return {"system": dict(native_evidence["system"], name=system.Name),
                    "proposed_name": "SUPPLY-01", "name_conflicts": []}

        reader.read_system_name_repair = read_name

    class Group:
        def __init__(self, *_):
            self.state = "Uninitialized"

        def Start(self):
            self.state = "Started"
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            calls.append("group_rollback")
            if fault != "survives":
                current[:] = [dict(row) for row in original]
                system.Name = "Old"
            self.state = "RolledBack"
            return self.state

    class Transaction(Group):
        def RollBack(self):
            calls.append("transaction_rollback")
            self.state = "RolledBack"
            return self.state

        def Commit(self):
            if fault == "pending":
                self.state = "Pending"
                return self.state
            identifiers = [31, int(LARGE_ID)] if rename_method else [int(LARGE_ID)]
            if fault == "extra":
                identifiers[-1] = 23
            transaction_name = module["RENAME_TRANSACTION_NAME" if rename_method else "TRANSACTION_NAME"]
            event = NS(Operation="TransactionCommitted", GetTransactionNames=lambda: [transaction_name],
                       GetDocument=lambda: object() if fault == "foreign" else document,
                       GetAddedElementIds=Collection, GetDeletedElementIds=Collection,
                       GetModifiedElementIds=lambda: Collection(NS(Value=identifier) for identifier in identifiers))
            document.Application.DocumentChanged.callback(None, event)
            self.state = "Committed"
            return self.state

    db.TransactionGroup, db.Transaction = Group, Transaction
    result = module["trial"](document, args, reader)
    assert result["status"] == outcome, result
    assert not hasattr(document.Application.DocumentChanged, "callback")
    if fault == "pending":
        assert not calls
    else:
        assert calls[-1] == "group_rollback"
    if fault not in ("pending", "survives"):
        assert current == original
    if rename_method and fault not in ("pending", "survives"):
        assert system.Name == "Old"
    if fault is None:
        modified = ["31", LARGE_ID] if rename_method else [LARGE_ID]
        assert result["trial_changes"] == {"added": [], "modified": modified, "deleted": []}
        if rename_method:
            assert result["rename_method"] == rename_method


@pytest.mark.parametrize("fault", [None, "reused_run", "changed_readback", "extra_effect", "wrong_preview"])
def test_independent_trial_verifier_requires_exact_effects_and_restoration(inputs, tmp_path, fault):
    evidence, run = inputs
    before = run()
    assert before.returncode == 0, before.stderr
    display = json.loads(before.stdout)
    query = tmp_path / "query.json"
    fill = tmp_path / "fill.json"
    original = json.loads(query.read_text())["response"]
    trial = dict(original, run_id="123456789abc", mode="dry_run", result={
        "status": "mep_text_fill_trial_rolled_back", "basis_sha256": display["basis_sha256"],
        "source_run_id": original["run_id"], "trial_changes": {"added": [], "modified": [LARGE_ID], "deleted": []},
    }, changes=dict(original["changes"], modified=[{"id": int(LARGE_ID)}], modified_count=1,
                    by_category={"Ducts": 1}, transactions=["Test reviewed MEP text fill"]))
    fresh = dict(original, run_id="23456789abcd", result=dict(original["result"]))
    if fault == "reused_run":
        fresh["run_id"] = original["run_id"]
    elif fault == "changed_readback":
        evidence["elements"][0]["value"] = "Not restored"
        fresh["result"]["repair_parameters_json"] = json.dumps(evidence)
    elif fault == "extra_effect":
        trial["changes"]["modified"] = [{"id": 23}]
    elif fault == "wrong_preview":
        trial["result"]["basis_sha256"] = "f" * 64
    trial_path, fresh_path = tmp_path / "trial.json", tmp_path / "fresh.json"
    trial_path.write_text(json.dumps(trial))
    fresh_path.write_text(json.dumps(fresh))

    result = subprocess.run([sys.executable, SCRIPT, query, fill, "--trial", trial_path, "--fresh", fresh_path],
                            capture_output=True, text=True)
    if fault is not None:
        assert result.returncode == 2 and not result.stdout
        assert {
            "reused_run": "three independent Revit run records",
            "changed_readback": "independent Revit readback differs",
            "extra_effect": "bridge change record disagrees",
            "wrong_preview": "does not match the reviewed preview",
        }[fault] in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {
            "status": "verified_mep_text_fill_trial_rollback", "basis_sha256": display["basis_sha256"],
            "original_run_id": original["run_id"], "trial_run_id": trial["run_id"], "fresh_run_id": fresh["run_id"],
            "modified_ids": [LARGE_ID],
        }


def test_system_name_preview_shows_members_and_blocks_collisions(inputs, tmp_path):
    _, run = inputs
    run()
    query = tmp_path / "query.json"
    record = json.loads(query.read_text())["response"]
    fill_evidence = json.loads(record["result"]["repair_parameters_json"])
    snapshot = system_name_evidence(fill_evidence["source"])

    def invoke():
        record["result"] = {"status": "ok", "repair_system_name_json": json.dumps(snapshot)}
        query.write_text(json.dumps(record))
        return subprocess.run([sys.executable, SCRIPT, query, "--system-name"], capture_output=True, text=True)

    result = invoke()
    assert result.returncode == 0, result.stderr
    display = json.loads(result.stdout)
    assert display["before"][0][-1] == "Old"
    assert display["after"][0][-1] == "SUPPLY-01"
    assert display["before"][1:] == display["after"][1:]
    assert [row[0] for row in display["after"][1:]] == ["22", LARGE_ID]
    assert display["review_only"] is True and display["steps"] == []

    snapshot["name_conflicts"] = [{"id": "32", "uid": "conflict", "name": "supply-01"}]
    blocked = invoke()
    assert blocked.returncode == 1
    assert json.loads(blocked.stdout)["warnings"][0]["text"] == "Name already used by system 32."

    snapshot["name_conflicts"] = []
    snapshot["system"]["members"][0]["assignments"][0]["system"] = {"id": "34", "uid": "different"}
    rejected = invoke()
    assert rejected.returncode == 2 and "matching connector assignment" in rejected.stderr
