import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "parameter_copy_preview.py"


def test_copy_combine_and_type_mismatch_through_cli(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    artifact = tmp_path / "artifact.json"
    eid = "9007199254740993"

    def field(pid, storage, spec, value, **extra):
        return {
            "identity": {"id": pid, "guid": None},
            "storage": storage,
            "spec": spec,
            "value": value,
            "has_value": True,
            "read_only": False,
            "global_controlled": False,
            **extra,
        }

    row = {"id": eid, "uid": "instance-one", "scheme": "primary", "grouped": False,
           "fields": {
               "Source": field("10", "String", "text", "Door"),
               "Target": field("11", "String", "text", ""),
               "Number": field("12", "Integer", "integer", 3, display="03"),
               "Level": field("-101", "ElementId", "", {"id": "1", "uid": "level-one", "label": "Level 1"},
                              display="Level 1"),
           }}
    request = {"category_id": "-2000014", "element_ids": [eid], "destination": "Target"}

    def preview(operation):
        basis = {"request": {**request, **operation}, "rows": [row]}
        snapshot.write_text(json.dumps({"response": {"status": "ok", "result": {
            "status": "ok", "snapshot_json": json.dumps(basis),
        }}}))
        subprocess.run([sys.executable, SCRIPT, "compute", snapshot, artifact], check=True, capture_output=True)
        return json.loads(artifact.read_text())

    copy = preview({"source": "Source"})
    assert copy["preview"]["before"] == [[eid, "(empty text)"]]
    assert copy["preview"]["after"] == [[eid, '"Door"']]
    assert copy["preview"]["ownership_ids"] == [eid]
    assert copy["preview"]["steps"] == []
    assert copy["plan"]["steps"] == [
        {"id": eid, "uid": "instance-one", "identity": {"id": "11", "guid": None},
         "storage": "String", "value": "Door"}
    ]

    combined = preview({"parts": [{"parameter": "Level"}, {"literal": " / "}, {"parameter": "Number"}]})
    assert combined["preview"]["after"] == [[eid, '"Level 1 / 03"']]
    assert combined["plan"]["steps"][0]["value"] == "Level 1 / 03"
    good = [sys.executable, SCRIPT, "baseline", artifact, snapshot]
    subprocess.run(good, check=True, capture_output=True)

    row["fields"]["Source"]["value"] = "(empty text)"
    literal = preview({"source": "Source"})
    assert literal["preview"]["before"] == [[eid, "(empty text)"]]
    assert literal["preview"]["after"] == [[eid, '"(empty text)"']]
    row["fields"]["Source"]["value"] = "Door"

    request["destination"] = "Number"
    blocked = preview({"source": "Source"})
    assert blocked["preview"]["warnings"] == [
        {"row": eid, "blocking": True, "text": "Source and destination have different storage types or Revit data types."}
    ]
    assert blocked["preview"]["blocked"] is True
    assert blocked["preview"]["ownership_ids"] == []
    assert blocked["plan"]["steps"] == []
    row["fields"]["Source"]["value"] = "Changed since review"
    basis = {"request": {**request, "source": "Source"}, "rows": [row]}
    snapshot.write_text(json.dumps({"status": "ok", "snapshot_json": json.dumps(basis)}))
    assert subprocess.run(good, capture_output=True).returncode == 2


def test_native_entry_preflight_and_rollback(monkeypatch):
    import runpy
    from types import SimpleNamespace as NS

    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    from parameter_copy_preview import compare_kept, compute, verify_dry_run

    class Id:
        def __init__(self, value):
            self.Value = int(value)

        def __eq__(self, other):
            return isinstance(other, Id) and self.Value == other.Value

    Id.InvalidElementId = Id(-1)
    category = NS(Id=Id(-2000014), Name="Doors")
    events = []

    class Parameter:
        def __init__(self, pid, value, fails=False):
            self.Id, self.value, self.fails = Id(pid), value, fails
            self.StorageType = NS(ToString=lambda: "String")
            self.Definition = NS(GetDataType=lambda: NS(TypeId="text"))
            self.IsShared = self.IsReadOnly = False
            self.HasValue = True

        def GetAssociatedGlobalParameter(self):
            return Id.InvalidElementId

        def AsString(self):
            return self.value

        def Set(self, value):
            events.append(("set", value))
            if self.fails:
                return False
            self.value = value
            return True

    def element(eid, target):
        fields = {"Source": Parameter(10, "New"), "Target": target}
        return NS(Id=Id(eid), UniqueId="id-" + str(eid), Category=category,
                  GroupId=Id.InvalidElementId, DesignOption=None,
                  GetParameters=lambda name: [fields[name]] if name in fields else [])

    first, second = Parameter(11, "Old one"), Parameter(11, "Old two", fails=True)
    elements = {1: element(1, first), 2: element(2, second)}
    status = NS(Started="Started", Committed="Committed", RolledBack="RolledBack")

    class Transaction:
        def __init__(self, _doc, _name):
            self.old = first.value, second.value

        def Start(self):
            events.append("start")
            return status.Started

        def GetStatus(self):
            return status.Started

        def RollBack(self):
            first.value, second.value = self.old
            events.append("rollback")
            return status.RolledBack

        def Commit(self):
            events.append("commit")
            return status.Committed

    db = NS(ElementId=Id, ElementType=type("ElementType", (), {}), Transaction=Transaction,
            TransactionStatus=status)
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int32=int, Int64=int, Double=float))
    doc = NS(IsFamilyDocument=False, IsReadOnly=False, IsWorkshared=False, PathName="/example/project.rvt",
             ProjectInformation=NS(UniqueId="project-one"), GetElement=lambda eid: elements.get(eid.Value),
             Regenerate=lambda: events.append("regenerate"))

    def run(args):
        return runpy.run_path(str(SCRIPT.with_name("parameter_copy.py")), init_globals={"doc": doc, "inputs": args})["result"]

    request = {"category_id": "-2000014", "element_ids": ["1", "2"], "source": "Source", "destination": "Target"}
    read = run({"mode": "read", "request": request})
    artifact = compute(json.loads(read["snapshot_json"]))
    assert not events
    proposed = {"mode": "apply", **artifact}
    blocked = {"mode": "apply", **artifact, "preview": dict(artifact["preview"], blocked=True)}
    assert run(blocked)["status"] == "stopped"
    assert not events
    result = run(proposed)
    assert result["status"] == "stopped"
    assert events == ["start", ("set", "New"), ("set", "New"), "rollback"]
    assert (first.value, second.value) == ("Old one", "Old two")

    events.clear()
    second.fails = False
    kept = run(proposed)
    assert kept["status"] == "ok"
    assert events[-2:] == ["regenerate", "commit"]
    assert (first.value, second.value) == ("New", "New")
    events.clear()
    assert run(proposed)["status"] == "stopped"
    assert not events

    compare_kept(artifact["plan"], json.loads(run({"mode": "read", "request": request})["snapshot_json"]))
    dry = {"mode": "dry_run", "status": "ok", "decision": "rolled_back", "run_id": "dry-copy",
           "exit_code": 0, "result_truncated": False,
           "result": kept, "changes": {"added_count": 0, "deleted_count": 0,
           "modified_count": 2, "modified": [{"id": 1}, {"id": 2}], "by_category": {"Doors": 2}}}
    for invalid in (dict(dry, mode="modify", decision="committed", approval="user"),
                    dict(dry, status="error"), dict(dry, result_truncated=True),
                    dict(dry, run_id=""), dict(dry, exit_code=1)):
        with pytest.raises(ValueError, match="successful rolled-back dry run"):
            verify_dry_run(artifact["plan"], invalid)
    verify_dry_run(artifact["plan"], {"response": dry})
    dry["changes"].update(modified_count=3, modified=[{"id": 1}, {"id": 2}, {"id": 3}],
                          by_category={"Doors": 3})
    with pytest.raises(ValueError, match="additional modified elements: 3"):
        verify_dry_run(artifact["plan"], dry)
