"""Exercise sheet number reconciliation and the read-only Revit entry point."""

import hashlib
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sheet_creation_preview as preview


def id_value(value):
    return SimpleNamespace(Value=value)


def row(number, name, source_row, type_id="80", parameters=None):
    return {"number": number, "name": name, "source_row": source_row,
            "title_block_type_id": type_id, "parameters": parameters or []}


def snapshot(rows, sheets=()):
    return {"kind": "sheet-creation-snapshot", "complete": True,
            "document": {"project_uid": "abc-123", "path_sha256": "a" * 64},
            "request": {"rows": rows}, "sheets": list(sheets),
            "title_blocks": [{"id": "80", "uid": "aa-80", "family": "Standard", "name": "A1"}]}


def sheet(number, name, id, *, placeholder=False, parameters=None):
    return {"id": id, "uid": f"ad-{id}", "number": number, "name": name,
            "placeholder": placeholder, "title_block_type_id": "80", "parameters": parameters}


def test_preview_reports_existing_numbers_without_proposing_overwrites():
    params = [{"scope": "title_block", "parameter": {"kind": "guid", "id": "12345678-1234-1234-1234-123456789abc"},
               "kind": "double", "value": 25, "unit": "millimeters"}]
    request = [row("001", "New", 2, parameters=params), row("A-101", "Same", 3),
               row("A-102", "Replace", 4), row("a-103", "Case conflict", 5),
               row("A-104", "Placeholder conflict", 6)]
    current = [sheet("A-101", "Same", "11"), sheet("A-102", "Keep existing", "12"),
               sheet("A-103", "Case existing", "13"), sheet("A-104", "Placeholder", "14", placeholder=True)]
    result = preview.compute(snapshot(request, current))
    assert not result["blocked"] and result["ownership_ids"] == []
    assert [item["number"] for item in result["steps"]] == ["001"]
    assert result["steps"][0]["title_block_type_uid"] == "aa-80"
    assert result["steps"][0]["parameters"] == params
    assert [item[-1] for item in result["after"]] == [
        "Create", "Already matches", "Skip number conflict", "Skip number conflict", "Skip number conflict",
    ]
    assert result["after"][2][2] == "Keep existing"
    assert result["after"][3][1] == "A-103"
    assert [item[5] for item in result["after"][1:]] == ["11", "12", "13", "14"]
    replay = preview.compute(snapshot(request, current + [sheet("001", "New", "15", parameters=params)]))
    assert replay["steps"] == [] and replay["after"][0][-1] == "Already matches"


def test_non_ascii_duplicate_and_missing_type_block_the_whole_batch():
    request = [row("É-1", "First", 1), row("e\u0301-1", "Second", 2), row("001", "Valid", 3),
               row("002", "Unknown type", 4, "999")]
    result = preview.compute(snapshot(request))
    assert result["blocked"] and result["steps"] == []
    assert [warning["row"] for warning in result["warnings"]] == ["1", "2", "4"]


def test_preview_cli_binds_request_and_original_inventory(tmp_path):
    item = snapshot([row("001", "Cover", 3)])
    item["request"]["source"] = {"sha256": "f" * 64, "file": "index.xlsx", "sheet": "Drawings",
                                 "header_row": 2, "mapping": {"number": "Sheet Number"}}
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"result": {"status": "ok", "snapshot_json": json.dumps(item)}}))
    report = json.loads(subprocess.check_output([sys.executable, SCRIPTS / "sheet_creation_preview.py", path]))
    assert report["request"]["source"] == item["request"]["source"]
    assert report["basis_sha256"] == hashlib.sha256(preview.encode(item).encode("ascii")).hexdigest()
    item["document"]["path_sha256"] = "b" * 64
    assert preview.compute(item)["basis_sha256"] != report["basis_sha256"]
    item["complete"] = False
    with pytest.raises(ValueError, match="complete"):
        preview.compute(item)


@pytest.mark.parametrize("number", [1, " 001 ", "A[1]"])
def test_rejects_invalid_sheet_numbers(number):
    with pytest.raises(ValueError):
        preview.compute(snapshot([row(number, "Cover", 1)]))


@pytest.fixture
def sheet_host(monkeypatch):
    class ViewSheet(SimpleNamespace):
        @staticmethod
        def Create(doc, type_id):
            index = doc.next_id
            doc.next_id += 2
            sheet = ViewSheet(Id=id_value(index), UniqueId=f"aa-{index}", SheetNumber="", Name="",
                              IsPlaceholder=False)
            block = FamilyInstance(Id=id_value(index + 1), UniqueId=f"aa-{index + 1}", OwnerViewId=sheet.Id,
                                   type_id=type_id, parameter=Parameter())
            doc.sheets.append(sheet)
            doc.blocks.append(block)
            return sheet

    class FamilySymbol(SimpleNamespace):
        pass

    class FamilyInstance(SimpleNamespace):
        def GetTypeId(self):
            return self.type_id

        def get_Parameter(self, _identifier):
            return self.parameter

    class Parameter:
        Id = id_value(33)
        IsReadOnly = False
        StorageType = SimpleNamespace(ToString=lambda: "String")
        Definition = SimpleNamespace(GetDataType=lambda: "Text")

        def __init__(self):
            self.value = ""

        def GetAssociatedGlobalParameter(self):
            return id_value(-1)

        def AsString(self):
            return self.value

        def Set(self, value):
            self.value = value
            return True

    class Collector:
        def __init__(self, doc):
            self.doc = doc
            self.rows = []

        def OfClass(self, cls):
            self.rows = {ViewSheet: self.doc.sheets, FamilySymbol: self.doc.types}[cls]
            return self

        def OwnedByView(self, owner):
            self.rows = [block for block in self.doc.blocks if block.OwnerViewId.Value == owner.Value]
            return self

        def OfCategory(self, category):
            assert category == "title-blocks"
            return self

        def WhereElementIsNotElementType(self):
            return self

        def __iter__(self):
            return iter(self.rows)

    status = SimpleNamespace(Started="started", Committed="committed", RolledBack="rolled_back", Pending="pending")

    class Event:
        def __init__(self):
            self.handler = None

        def __iadd__(self, callback):
            assert self.handler is None
            self.handler = callback
            return self

        def __isub__(self, callback):
            assert self.handler == callback
            self.handler = None
            return self

    class Group:
        def __init__(self, doc, label):
            self.doc, self.state = doc, "new"

        def Start(self):
            self.doc.groups += 1
            self.doc.original = self.doc.sheets[:], self.doc.blocks[:]
            self.state = status.Started
            return self.state

        def GetStatus(self):
            return self.state

        def Assimilate(self):
            self.state = status.Committed
            return self.state

        def RollBack(self):
            self.doc.sheets, self.doc.blocks = self.doc.original
            self.state = status.RolledBack
            return self.state

    class Ids(list):
        @property
        def Count(self):
            return len(self)

    class Transaction(Group):
        def __init__(self, doc, label):
            super().__init__(doc, label)
            self.label = label

        def Start(self):
            self.state = status.Started
            return self.state

        def Commit(self):
            doc = self.doc
            existing_sheets, existing_blocks = doc.original
            if doc.late_value:
                doc.blocks[-1].parameter.value = "changed during commit"
            added = Ids([item.Id for item in doc.sheets[len(existing_sheets):] + doc.blocks[len(existing_blocks):]])
            modified = Ids([doc.types[0].Id] if doc.extra_change else [])
            event = SimpleNamespace(GetDocument=lambda: doc, GetTransactionNames=lambda: [self.label],
                                    GetAddedElementIds=lambda: added, GetModifiedElementIds=lambda: modified,
                                    GetDeletedElementIds=lambda: Ids())
            doc.Application.DocumentChanged.handler(None, event)
            self.state = status.Committed
            return self.state

    cover = ViewSheet(Id=id_value(11), UniqueId="ac-11", SheetNumber="A-101", Name="Cover", IsPlaceholder=False)
    reserved = ViewSheet(Id=id_value(12), UniqueId="ac-12", SheetNumber="001", Name="Reserved", IsPlaceholder=True)
    symbol = FamilySymbol(Id=id_value(80), UniqueId="aa-80", FamilyName="Standard", Name="A1")
    block = FamilyInstance(Id=id_value(81), UniqueId="aa-81", OwnerViewId=cover.Id, type_id=symbol.Id,
                           parameter=Parameter())
    doc = SimpleNamespace(IsFamilyDocument=False, IsReadOnly=False, PathName="/tmp/qualification.rvt",
                          ProjectInformation=SimpleNamespace(UniqueId="aa-123"), next_id=200,
                          sheets=[cover, reserved], types=[symbol], blocks=[block], groups=0,
                          late_value=False, extra_change=False, Regenerate=lambda: None,
                          Application=SimpleNamespace(DocumentChanged=Event()))
    doc.Equals = lambda other: doc is other
    doc.GetElement = lambda ident: next((item for item in doc.sheets + doc.blocks + doc.types
                                        if item.Id.Value == ident.Value), None)
    db = SimpleNamespace(FilteredElementCollector=Collector, ViewSheet=ViewSheet, FamilySymbol=FamilySymbol,
                         FamilyInstance=FamilyInstance, BuiltInCategory=SimpleNamespace(OST_TitleBlocks="title-blocks"),
                         Element=SimpleNamespace(Name=SimpleNamespace(GetValue=lambda item: item.Name)),
                         ElementId=id_value, Transaction=Transaction, TransactionGroup=Group, TransactionStatus=status)
    system, api = ModuleType("System"), ModuleType("pyrevit.api")
    system.Int64, system.Guid = int, str
    api.DB = db
    monkeypatch.setitem(sys.modules, "System", system)
    diagnostics = ModuleType("System.Diagnostics")
    diagnostics.Stopwatch = object
    monkeypatch.setitem(sys.modules, "System.Diagnostics", diagnostics)
    monkeypatch.setitem(sys.modules, "pyrevit.api", api)
    duplicate_views = ModuleType("duplicate_views")
    duplicate_views.rollback = runpy.run_path(str(SCRIPTS / "duplicate_views.py"))["rollback"]
    monkeypatch.setitem(sys.modules, "duplicate_views", duplicate_views)
    native = runpy.run_path(str(SCRIPTS / "create_sheets.py"))
    assignment = {"scope": "title_block", "parameter": {"kind": "guid", "id": "12345678-1234-1234-1234-123456789abc"},
                  "kind": "string", "value": "Issue for review"}
    request = {"rows": [row("001", "New cover", 1), row("A-101", "Cover", 2),
                        row("002", "Plan", 3, parameters=[assignment])]}
    return doc, native["main"], request


def test_native_read_keeps_placeholders_and_identifies_matching_title_block(sheet_host):
    doc, main, request = sheet_host
    data = json.loads(main(doc, {"request": request})["snapshot_json"])
    plan = preview.compute(data)
    assert [item[-1] for item in plan["after"]] == ["Skip number conflict", "Already matches", "Create"]
    assert [item["number"] for item in plan["steps"]] == ["002"]


def test_sheet_creation_rechecks_kept_objects_and_replay_is_a_noop(sheet_host):
    doc, main, request = sheet_host
    plan = preview.compute(json.loads(main(doc, {"request": request})["snapshot_json"]))
    receipt = json.loads(main(doc, {"mode": "apply", "plan": plan})["receipt_json"])
    assert receipt["created"] == [{"sheet_id": "200", "sheet_uid": "aa-200",
                                   "title_block_id": "201", "title_block_uid": "aa-201"}]
    assert doc.blocks[-1].parameter.value == "Issue for review"
    verified = main(doc, {"mode": "verify", "plan": plan, "receipt": receipt})
    assert json.loads(verified["verification_json"]) == receipt
    replay = preview.compute(json.loads(main(doc, {"request": request})["snapshot_json"]))
    assert replay["steps"] == [] and replay["after"][-1][-1] == "Already matches"
    assert main(doc, {"mode": "apply", "plan": replay})["status"] == "no_changes"
    assert doc.groups == 1
    assert [sheet.SheetNumber for sheet in doc.sheets] == ["A-101", "001", "002"]


@pytest.mark.parametrize("mutation", ["late_value", "extra_change"])
def test_sheet_creation_rolls_back_if_commit_changes_unreviewed_state(sheet_host, mutation):
    doc, main, request = sheet_host
    baseline = json.loads(main(doc, {"request": request})["snapshot_json"])
    setattr(doc, mutation, True)
    outcome = main(doc, {"mode": "apply", "plan": preview.compute(baseline)})
    assert outcome["status"] == "stopped"
    assert json.loads(main(doc, {"request": request})["snapshot_json"]) == baseline
    assert len(doc.blocks) == 1 and doc.Application.DocumentChanged.handler is None


def runtime_record(payload, key, *, decision="rolled_back", added=()):
    return {"status": "ok", "decision": decision, "approval": "user",
            "result": {"status": "ok", key: preview.encode(payload)},
            "changes": {"added_ids": list(added), "added_count": len(added),
                        "modified_count": 0, "deleted_count": 0,
                        "by_category": {"Sheets": 1, "Title Blocks": 1} if added else {}}}


def test_full_runtime_receipts_require_exact_additions_and_fresh_readback(sheet_host):
    doc, main, request = sheet_host
    baseline = json.loads(main(doc, {"request": request})["snapshot_json"])
    plan = preview.compute(baseline)
    receipt = json.loads(main(doc, {"mode": "apply", "plan": plan})["receipt_json"])
    dry_receipt = {**receipt, "created": [{"sheet_id": "700", "sheet_uid": "aa-700",
                                          "title_block_id": "701", "title_block_uid": "aa-701"}]}
    dry = runtime_record(dry_receipt, "receipt_json", added=("700", "701"))
    fresh = runtime_record(baseline, "snapshot_json")
    assert preview.verify_outcome(plan, dry, fresh, "rolled_back") == dry_receipt
    dry["changes"]["added_ids"][-1] = "999"
    with pytest.raises(ValueError, match="exact reviewed"):
        preview.verify_outcome(plan, dry, fresh, "rolled_back")
    dry["changes"]["added_ids"][-1] = "701"
    kept = runtime_record(receipt, "receipt_json", decision="committed", added=("200", "201"))
    readback = json.loads(main(doc, {"mode": "verify", "plan": plan, "receipt": receipt})["verification_json"])
    assert preview.verify_outcome(plan, kept, runtime_record(readback, "verification_json"),
                                  "committed", dry) == receipt
    with pytest.raises(ValueError, match="independently read"):
        preview.verify_outcome(plan, kept, runtime_record(dry_receipt, "verification_json"), "committed", dry)
