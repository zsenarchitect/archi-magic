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


@pytest.mark.parametrize("fault, outcome", [
    (None, "mep_text_fill_trial_rolled_back"),
    ("extra", "stopped_and_rolled_back"),
    ("setter_failed", "stopped_and_rolled_back"),
    ("foreign", "transaction_unresolved"),
    ("pending", "transaction_unresolved"),
    ("survives", "transaction_unresolved"),
])
def test_native_fill_trial_measures_effects_and_always_rolls_back(inputs, tmp_path, monkeypatch, fault, outcome):
    import runpy
    from types import SimpleNamespace as NS

    from test_probe_mep_route import Collection, Handler, RegenerationFailure

    evidence, run = inputs
    display = json.loads(run().stdout)
    args = {"disposable_fixture": True, "query": json.loads((tmp_path / "query.json").read_text()),
            "fill": json.loads((tmp_path / "fill.json").read_text()), "preview": display}
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
            identifier = 23 if fault == "extra" else int(LARGE_ID)
            event = NS(Operation="TransactionCommitted", GetTransactionNames=lambda: [module["TRANSACTION_NAME"]],
                       GetDocument=lambda: object() if fault == "foreign" else document,
                       GetAddedElementIds=Collection, GetDeletedElementIds=Collection,
                       GetModifiedElementIds=lambda: Collection([NS(Value=identifier)]))
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
    if fault is None:
        assert result["trial_changes"] == {"added": [], "modified": [LARGE_ID], "deleted": []}
