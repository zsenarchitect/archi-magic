import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_sheets.py"


def eid(value):
    return NS(Value=value)


class Sheet:
    def __init__(self, value, scheduled, additional, clouds, placeholder=False):
        self.Id = eid(value)
        self.UniqueId = "sheet-" + str(value)
        self.SheetNumber, self.Name = "A" + str(value), "Fixture sheet"
        self.IsPlaceholder = placeholder
        self.scheduled, self.additional, self.clouds = scheduled, additional, clouds

    def GetAllRevisionIds(self):
        return list(map(eid, self.scheduled))

    def GetAdditionalRevisionIds(self):
        return list(map(eid, self.additional))

    def SetAdditionalRevisionIds(self, values):
        self.additional = [item.Value for item in values]

    def GetAllRevisionCloudIds(self):
        return list(map(eid, self.clouds))

    def GetRevisionNumberOnSheet(self, identifier):
        if identifier.Value == 8:
            return ""  # A revision with no numbering scheme.
        return str(self.scheduled.index(identifier.Value) + 1)


class Cloud:
    def __init__(self, value, revision, owner, sheets, comment):
        self.Id, self.RevisionId, self.OwnerViewId = eid(value), eid(revision), eid(owner)
        self.UniqueId = "cloud-" + str(value)
        self.sheets, self.comment = sheets, comment

    def GetSheetIds(self):
        return list(map(eid, self.sheets))

    def get_Parameter(self, _identifier):
        return NS(StorageType="text", AsString=lambda: self.comment)


class Revision:
    def __init__(self, value, issued=False):
        self.UniqueId, self.SequenceNumber = "revision-" + str(value), value
        self.Description, self.RevisionDate, self.Issued = "Fixture revision", "2026-11-11", issued


@pytest.fixture
def inventory(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.setitem(sys.modules, "read_sheets", None)
    monkeypatch.delitem(sys.modules, "read_sheets")
    sheet_ids = [9007199254740993, 102, 103]
    a, b, c = sheet_ids
    sheets = [Sheet(a, [5, 6], [6], [51]), Sheet(b, [5], [], [52]), Sheet(c, [], [], []),
              Sheet(104, [], [], [], placeholder=True)]
    clouds = [Cloud(51, 5, a, [], "Café – boundary"), Cloud(52, 5, 200, [b, c], "Fixture cloud"),
              Cloud(53, 7, c, [], "Hidden issue candidate"), Cloud(54, 7, 202, [999], "Out of scope")]
    elements = {item.Id.Value: item for item in sheets + clouds}
    elements.update({5: Revision(5, issued=True), 6: Revision(6), 7: Revision(7), 8: Revision(8)})
    db = NS(ViewSheet=Sheet, RevisionCloud=Cloud, Revision=Revision, ElementId=eid,
            BuiltInParameter=NS(ALL_MODEL_INSTANCE_COMMENTS="comments"), StorageType=NS(String="text"),
            FilteredElementCollector=lambda _doc: NS(OfClass=lambda kind: clouds if kind is Cloud else sheets,
                                                       Dispose=lambda: None))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))))
    class RevisionList(list):
        Add = list.append

        @classmethod
        def __class_getitem__(cls, item):
            assert item is eid
            return cls

    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=RevisionList))
    db.TransactionStatus = NS(Started="started", Committed="committed", RolledBack="rolled_back")

    class Transaction:
        def __init__(self, doc, title):
            self.state = None
            self.baseline = [(sheet.additional[:], sheet.scheduled[:]) for sheet in sheets]
            doc.transactions.append(self)

        def Start(self):
            self.state = "started"
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            for sheet, (additional, scheduled) in zip(sheets, self.baseline):
                sheet.additional, sheet.scheduled = additional, scheduled
            self.state = "rolled_back"
            return self.state

        def Commit(self):
            self.state = "committed"
            return self.state

    db.Transaction = Transaction

    def regenerate():
        for sheet in sheets:
            scheduled_clouds = {cloud.RevisionId.Value for cloud in clouds if cloud.Id.Value in sheet.clouds}
            sheet.scheduled = sorted(set(sheet.additional) | scheduled_clouds)

    document = NS(Title="Fixture", IsFamilyDocument=False, IsReadOnly=False, PathName="synthetic.rvt",
                  Regenerate=regenerate, transactions=[],
                  ProjectInformation=NS(UniqueId="fixture-project"), GetElement=lambda item: elements.get(item.Value))

    def execute(**inputs):
        return runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": inputs})["result"]

    execute.document = document
    return execute, list(map(str, sheet_ids)), sheets, clouds


def test_reconciles_three_native_sheet_schedules_with_explicit_and_potential_clouds(inventory):
    execute, ids, _, _ = inventory
    result = execute(revision_sheet_ids=ids, revision_ids=["8"])
    assert result["status"] == "ok" and result["snapshot_json"].isascii()
    report = json.loads(result["snapshot_json"])
    a, b, c = (next(row for row in report["sheets"] if row["id"] == value) for value in ids)
    assert a["scheduled"] == [{"revision_id": "5", "number": "1"}, {"revision_id": "6", "number": "2"}]
    assert a["additional_revision_ids"] == ["6"] and a["scheduled_cloud_ids"] == ["51"]
    assert a["clouds"] == [{"id": "51", "uid": "cloud-51", "revision_id": "5", "owner_view_id": ids[0],
                            "comments": "Café – boundary", "scheduled": True, "revision_on_sheet": True}]
    assert b["scheduled_cloud_ids"] == ["52"] and b["clouds"][0]["scheduled"] is True
    assert c["scheduled"] == [] and [(row["id"], row["scheduled"], row["revision_on_sheet"]) for row in c["clouds"]] == [
        ("52", False, False), ("53", False, False),
    ]
    assert [row["id"] for row in report["revisions"]] == ["5", "6", "7", "8"]
    assert report["revisions"][0]["issued"] is True


def test_missing_native_scheduled_cloud_stops_without_partial_result(inventory):
    execute, ids, _, clouds = inventory
    clouds.pop(0)
    result = execute(revision_sheet_ids=ids)
    assert result == {"status": "stopped", "message": "The sheet's scheduled cloud inventory could not be reconciled."}


def test_revision_scope_keeps_placeholders_in_legacy_inventory_only(inventory):
    execute, _, _, _ = inventory
    assert any(row["placeholder"] for row in execute()["sheets"])
    assert execute(revision_sheet_ids=["104"])["status"] == "stopped"
    assert execute(revision_sheet_ids=["102", "102"])["status"] == "stopped"


def revision_preview(snapshot, intent, tmp_path):
    source, request = tmp_path / "snapshot.json", tmp_path / "intent.json"
    source.write_text(json.dumps({"response": {"status": "ok", "result": snapshot}}))
    request.write_text(json.dumps(intent))
    return subprocess.run([sys.executable, str(SCRIPT.with_name("sheet_revision_preview.py")), str(source), str(request)],
                          text=True, capture_output=True)


def test_preview_removes_manual_revision_without_erasing_cloud_schedule(inventory, tmp_path):
    execute, ids, sheets, _ = inventory
    sheets[0].additional = [5, 6]
    sheets[2].scheduled = sheets[2].additional = [5]
    result = revision_preview(execute(revision_sheet_ids=ids), {"operation": "remove", "revision_id": "5"}, tmp_path)
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    steps = {step["sheet_id"]: step for step in preview["steps"]}
    assert preview["ownership_ids"] == [ids[2], ids[0]]
    assert steps[ids[0]] == {"sheet_id": ids[0], "sheet_uid": "sheet-" + ids[0],
                             "additional_revision_ids": ["6"], "scheduled_revision_ids": ["5", "6"]}
    assert steps[ids[2]]["additional_revision_ids"] == [] and steps[ids[2]]["scheduled_revision_ids"] == []
    after = {row[0]: row[-2:] for row in preview["after"]}
    assert after == {ids[0]: [False, True], ids[1]: [False, True], ids[2]: [False, False]}
    assert {warning["row"] for warning in preview["warnings"]} == {ids[0], ids[1]}


def test_preview_adds_explicit_revision_and_rejects_unreconciled_cloud_evidence(inventory, tmp_path):
    execute, ids, _, _ = inventory
    read = execute(revision_sheet_ids=ids, revision_ids=["8"])
    intent = {"operation": "add", "revision_id": "8"}
    result = revision_preview(read, intent, tmp_path)
    assert result.returncode == 0, result.stderr
    preview = json.loads(result.stdout)
    assert [row["additional_revision_ids"] for row in preview["steps"]] == [["8"], ["8"], ["6", "8"]]
    assert [row["scheduled_revision_ids"] for row in preview["steps"]] == [["5", "8"], ["8"], ["5", "6", "8"]]
    snapshot = json.loads(read["snapshot_json"])
    snapshot["sheets"][0]["scheduled_cloud_ids"] = []
    result = revision_preview(dict(read, snapshot_json=json.dumps(snapshot)), intent, tmp_path)
    assert result.returncode == 2 and not result.stdout


def apply_revision(execute, plan):
    path = SCRIPT.with_name("apply_sheet_revisions.py")
    return runpy.run_path(str(path), init_globals={"doc": execute.document, "inputs": {"preview": plan}})["result"]


@pytest.mark.parametrize("operation,target", [("add", "8"), ("remove", "5")])
def test_apply_checks_native_membership_and_preserves_other_revisions(inventory, tmp_path, operation, target):
    execute, ids, sheets, _ = inventory
    sheets[0].additional = [5, 6]
    sheets[2].additional, sheets[2].scheduled = [5], [5]
    read = execute(revision_sheet_ids=ids, revision_ids=["8"])
    plan = json.loads(revision_preview(read, {"operation": operation, "revision_id": target}, tmp_path).stdout)
    plan["ownership"] = {"checked": len(plan["steps"])}
    plan["notes"].append("Ownership checked.")
    result = apply_revision(execute, plan)
    assert result["status"] == "ok"
    assert [txn.state for txn in execute.document.transactions] == ["committed"]
    post = execute(revision_sheet_ids=ids, revision_ids=["8"])
    assert result["snapshot_json"] == post["snapshot_json"]
    if operation == "add":
        assert [sheet.additional for sheet in sheets[:3]] == [[5, 6, 8], [8], [5, 8]]
        assert [sheet.scheduled for sheet in sheets[:3]] == [[5, 6, 8], [5, 8], [5, 8]]
    else:
        assert [sheet.additional for sheet in sheets[:3]] == [[6], [], []]
        assert [sheet.scheduled for sheet in sheets[:3]] == [[5, 6], [5], []]
    source, record = tmp_path / "kept.json", tmp_path / "preview.json"
    source.write_text(json.dumps(post))
    record.write_text(json.dumps(plan))
    verification = subprocess.run(
        [sys.executable, str(SCRIPT.with_name("sheet_revision_preview.py")), str(source), str(record), "--verify"],
        text=True, capture_output=True,
    )
    assert verification.returncode == 0, verification.stderr
    changes = {"added_count": 0, "deleted_count": 0, "modified_count": len(plan["steps"]),
               "by_category": {"Sheets": len(plan["steps"])},
               "modified": [{"id": int(value)} for value in plan["ownership_ids"]]}
    dry_run = {"mode": "dry_run", "status": "ok", "decision": "rolled_back",
               "run_id": "revision-dry-run", "result_truncated": False, "exit_code": 0, "changes": changes,
               "result": result}
    source.write_text(json.dumps(dry_run))
    command = [sys.executable, str(SCRIPT.with_name("sheet_revision_preview.py")), str(source), str(record),
               "--check-dry-run"]
    assert subprocess.run(command, text=True, capture_output=True).returncode == 0
    tampered = {**dry_run, "result": {**result, "changes": changes},
                "changes": {**changes, "deleted_count": 1, "deleted": [104]}}
    source.write_text(json.dumps({"response": tampered}))
    rejected = subprocess.run(command, text=True, capture_output=True)
    assert rejected.returncode == 2 and "Dry-run telemetry" in rejected.stderr
    assert "matches the preview" not in rejected.stdout
    for fields in ({"mode": "modify"}, {"decision": "committed"}, {"result_truncated": True}):
        source.write_text(json.dumps({**dry_run, **fields}))
        rejected = subprocess.run(command, text=True, capture_output=True)
        assert rejected.returncode == 2 and "matches the preview" not in rejected.stdout
    changes["modified"][0] = {"id": 104}
    source.write_text(json.dumps(dry_run))
    bad = subprocess.run(command, text=True, capture_output=True)
    assert bad.returncode == 2 and "did not identify every reviewed edit" in bad.stderr


def test_apply_stops_before_transaction_for_drift_or_changed_steps(inventory, tmp_path):
    execute, ids, sheets, clouds = inventory
    read = execute(revision_sheet_ids=ids, revision_ids=["8"])
    plan = json.loads(revision_preview(read, {"operation": "add", "revision_id": "8"}, tmp_path).stdout)
    plan["steps"][0]["additional_revision_ids"] = ["5"]
    with pytest.raises(ValueError, match="does not match"):
        apply_revision(execute, plan)
    plan["steps"][0]["additional_revision_ids"] = ["8"]
    clouds[0].comment = "Changed since review"
    with pytest.raises(ValueError, match="Model changed since the preview"):
        apply_revision(execute, plan)
    assert execute.document.transactions == []
    assert [sheet.additional for sheet in sheets[:3]] == [[6], [], []]


def test_wrong_revit_readback_rolls_back_every_sheet(inventory, tmp_path, monkeypatch):
    execute, ids, sheets, _ = inventory
    read = execute(revision_sheet_ids=ids, revision_ids=["8"])
    plan = json.loads(revision_preview(read, {"operation": "add", "revision_id": "8"}, tmp_path).stdout)
    monkeypatch.setattr(sheets[0], "SetAdditionalRevisionIds", lambda _values: None)
    with pytest.raises(ValueError, match="readback differs"):
        apply_revision(execute, plan)
    assert [txn.state for txn in execute.document.transactions] == ["rolled_back"]
    assert execute(revision_sheet_ids=ids, revision_ids=["8"]) == read
