import json
from pathlib import Path
import runpy
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

    def GetAllRevisionCloudIds(self):
        return list(map(eid, self.clouds))

    def GetRevisionNumberOnSheet(self, identifier):
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
    document = NS(Title="Fixture", IsFamilyDocument=False, PathName="synthetic.rvt",
                  ProjectInformation=NS(UniqueId="fixture-project"), GetElement=lambda item: elements.get(item.Value))

    def execute(**inputs):
        return runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": inputs})["result"]

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
