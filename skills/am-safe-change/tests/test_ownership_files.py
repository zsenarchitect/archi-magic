# -*- coding: utf-8 -*-
"""Tests for the ownership read and check by file (issue #278).

read_ownership.py runs here with small fake Revit objects. The tests check
the Python logic only (inputs_path, out_path, the summary, the escaping),
not the Revit behavior. ownership.py is tested with every accepted --read
file shape: bare result, run envelope, envelope with a string result,
Claude Code tool-results text blocks, and the file-mode summary.
"""
import importlib.util
import io
import json
import os
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
READ_SCRIPT = os.path.join(SCRIPTS, "read_ownership.py")


# --- fake Revit API ---------------------------------------------------------

class _Doc(object):
    def __init__(self, elements, categories=None, workshared=True):
        self.Title = "Project A"
        self.Application = NS(Username="me")
        self.IsFamilyDocument = False
        self.IsWorkshared = workshared
        self.elements = elements  # id -> dict(status, owner, updates, cat, name)
        self.Settings = NS(Categories=categories or [])

    def GetElement(self, key):
        if isinstance(key, str):
            for i, e in self.elements.items():
                if e.get("uid") == key:
                    return self._el(i)
            return None
        return self._el(key.Value) if key.Value in self.elements else None

    def _el(self, i):
        e = self.elements[i]
        return NS(Id=NS(Value=i), Name=e.get("name", "V"),
                  Category=NS(Name=e.get("cat", "Views")))


def _fake_db(doc):
    status = NS(OwnedByOtherUser="other", OwnedByCurrentUser="mine", NotOwned="free")
    updates = NS(CurrentWithCentral="current", UpdatedInCentral="updated",
                 NotYetInCentral="notyet", DeletedInCentral="deleted")
    to_status = {"OwnedByOtherUser": "other", "OwnedByCurrentUser": "mine", "NotOwned": "free"}
    to_updates = {"CurrentWithCentral": "current", "UpdatedInCentral": "updated"}

    def checkout(_doc, eid):
        return to_status[doc.elements[eid.Value].get("status", "NotOwned")]

    def tooltip(_doc, eid):
        return NS(Owner=doc.elements[eid.Value].get("owner", ""))

    def model_updates(_doc, eid):
        return to_updates[doc.elements[eid.Value].get("updates", "CurrentWithCentral")]

    def collector(_doc):
        def of_category(cat_id):
            return [doc._el(i) for i, e in sorted(doc.elements.items())
                    if e.get("cat", "Views") == cat_id]
        return NS(OfCategoryId=of_category)

    return NS(CheckoutStatus=status, ModelUpdatesStatus=updates,
              ElementId=lambda i: NS(Value=int(i)),
              WorksharingUtils=NS(GetCheckoutStatus=checkout,
                                  GetWorksharingTooltipInfo=tooltip,
                                  GetModelUpdatesStatus=model_updates),
              FilteredElementCollector=collector,
              SharedParameterElement=NS(Lookup=lambda d, g: None))


def run_read(monkeypatch, doc, inputs, platform=None):
    monkeypatch.setitem(sys.modules, "pyrevit", NS())
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=_fake_db(doc)))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    if platform:
        monkeypatch.setattr(sys, "platform", platform)
    try:
        return runpy.run_path(READ_SCRIPT, init_globals={"doc": doc, "inputs": inputs})["result"]
    finally:
        monkeypatch.undo()


def model():
    return _Doc({
        101: {"status": "OwnedByOtherUser", "owner": "Zoë", "cat": "Sheets", "name": "A1"},
        102: {"status": "OwnedByOtherUser", "owner": "Zoë", "cat": "Sheets", "name": "A2"},
        103: {"status": "OwnedByOtherUser", "owner": "user-b", "cat": "Sheets", "name": "A3"},
        104: {"status": "OwnedByCurrentUser", "cat": "Sheets", "name": "A4"},
        105: {"status": "NotOwned", "updates": "UpdatedInCentral", "cat": "Sheets", "name": "A5"},
        201: {"status": "OwnedByCurrentUser", "cat": "Views", "name": "Plan"},
    }, categories=[NS(Name="Views", Id="Views")])


def write(path, data):
    with io.open(str(path), "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def fwd(path):
    return str(path).replace("\\", "/")


# --- read_ownership.py ------------------------------------------------------

def test_inline_inputs_still_return_the_full_result(monkeypatch):
    res = run_read(monkeypatch, model(), {"ids": [101, 104, 999], "categories": ["Views"]})
    assert [e["id"] for e in res["elements"]] == [101, 104, 999]
    assert res["elements"][0]["owner"] == "Zoë"
    assert res["elements"][2] == {"id": 999, "exists": False}
    assert res["scan_borrowed"] == [{"id": 201, "category": "Views", "name": "Plan"}]
    assert "out_path" not in res


def test_inputs_path_and_out_path_write_full_result_and_return_summary(monkeypatch, tmp_path):
    inp = tmp_path / "own-inputs.json"
    write(inp, {"ids": [101, 102, 103, 104, 105, 999], "categories": ["Views", "Nope"],
                "max_elements": 5000})
    out = tmp_path / "sub" / "own.json"
    res = run_read(monkeypatch, model(), {"inputs_path": fwd(inp), "out_path": fwd(out)})
    assert "elements" not in res
    assert res["written"] is True and res["out_path"] == fwd(out)
    assert res["counts"] == {"elements": 6, "missing": 1, "id_changed": 0,
                             "owned_by_others": 3, "owned_by_me": 1, "not_owned": 1,
                             "updated_in_central": 1, "scan_borrowed": 1}
    assert res["owners"] == [["Zoë", 2], ["user-b", 1]]
    assert res["scan"] == {"checked": 1, "partial": False, "missing_categories": ["Nope"]}
    raw = io.open(str(out), encoding="utf-8").read()
    assert res["bytes"] == len(raw)
    full = json.loads(raw)
    assert full["escaped"] is False
    assert [e["id"] for e in full["elements"]] == [101, 102, 103, 104, 105, 999]
    assert full["elements"][0]["owner"] == "Zoë"
    assert len(json.dumps(res)) < 2000


def test_inputs_in_the_call_win_over_the_file(monkeypatch, tmp_path):
    inp = tmp_path / "own-inputs.json"
    write(inp, {"ids": [101, 102]})
    res = run_read(monkeypatch, model(), {"inputs_path": fwd(inp), "ids": [104]})
    assert [e["id"] for e in res["elements"]] == [104]


@pytest.mark.parametrize("bad", ["own.json", "work/own.json", "%TEMP%/own.json",
                                 "~/own.json", "C:own.json",
                                 pytest.param("/own.json", marks=pytest.mark.skipif(
                                     os.sep == "/", reason="drive-relative only on Windows"))])
def test_relative_out_path_is_refused_and_nothing_is_written(monkeypatch, tmp_path, bad):
    monkeypatch.chdir(tmp_path)
    res = run_read(monkeypatch, model(), {"ids": [101], "out_path": bad})
    assert res["status"] == "bad_inputs"
    assert "absolute path" in res["error"] and "#85" in res["error"]
    assert os.listdir(str(tmp_path)) == []


def test_unreadable_inputs_path_is_reported(monkeypatch, tmp_path):
    res = run_read(monkeypatch, model(), {"inputs_path": fwd(tmp_path / "missing.json")})
    assert res["status"] == "bad_inputs"
    assert "Cannot read inputs_path" in res["error"]
    assert run_read(monkeypatch, model(), {"inputs_path": "own-inputs.json"})["status"] == "bad_inputs"


def test_not_workshared_writes_the_file_too(monkeypatch, tmp_path):
    out = tmp_path / "own.json"
    doc = model()
    doc.IsWorkshared = False
    res = run_read(monkeypatch, doc, {"ids": [101], "out_path": fwd(out)})
    assert res["workshared"] is False and res["counts"]["elements"] == 0
    assert json.load(io.open(str(out), encoding="utf-8"))["elements"] == []


def test_ironpython_escapes_inline_text_but_the_file_holds_real_text(monkeypatch, tmp_path):
    inline = run_read(monkeypatch, model(), {"ids": [101]}, platform="cli")
    assert inline["escaped"] is True
    assert inline["elements"][0]["owner"] == "Zo\\u00eb"
    out = tmp_path / "own.json"
    res = run_read(monkeypatch, model(), {"ids": [101], "out_path": fwd(out)}, platform="cli")
    assert res["escaped"] is True and res["owners"] == [["Zo\\u00eb", 1]]
    full = json.load(io.open(str(out), encoding="utf-8"))
    assert full["escaped"] is False and full["elements"][0]["owner"] == "Zoë"


# --- ownership.py ----------------------------------------------------------

def _cli():
    spec = importlib.util.spec_from_file_location(
        "ownership_cli_files", os.path.join(SCRIPTS, "ownership.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _full():
    return {"document": "Project A", "workshared": True, "user": "me", "escaped": False,
            "elements": [{"id": 7, "exists": True, "status": "OwnedByOtherUser",
                          "owner": "user-b", "updates": "CurrentWithCentral",
                          "category": "Sheets", "name": "A1"},
                         {"id": 8, "exists": True, "status": "NotOwned", "owner": "",
                          "updates": "CurrentWithCentral", "category": "Sheets", "name": "A2"}],
            "scan": {"categories": [], "missing_categories": [], "checked": 0, "partial": False},
            "scan_borrowed": []}


def _shapes():
    full = _full()
    envelope = {"status": "ok", "run_id": "r1", "result": json.dumps(full)}
    return {
        "bare": full,
        "envelope_dict": {"status": "ok", "run_id": "r1", "result": full},
        "envelope_string": envelope,
        "tool_results_envelope": json.dumps(envelope),
        "tool_results_blocks": [{"type": "text", "text": json.dumps(envelope)}],
        "mcp_content": {"content": [{"type": "text", "text": json.dumps(envelope)}],
                        "isError": False},
    }


@pytest.mark.parametrize("shape", sorted(_shapes()))
def test_check_reads_every_accepted_file_shape(tmp_path, capsys, shape):
    cli = _cli()
    path = tmp_path / "own.json"
    data = _shapes()[shape]
    write(path, data if isinstance(data, str) else json.dumps(data))
    assert cli.main(["check", "--read", str(path)]) == 1
    out = capsys.readouterr().out
    assert "1 of 2 elements owned by others. Owners: user-b (1)." in out


def test_check_follows_the_summary_to_out_path(tmp_path, capsys):
    cli = _cli()
    own = tmp_path / "own.json"
    write(own, _full())
    summary = {"document": "Project A", "workshared": True, "out_path": fwd(own),
               "written": True, "counts": {"elements": 2}, "owners": [["user-b", 1]]}
    saved = tmp_path / "tool-result.json"
    write(saved, {"status": "ok", "run_id": "r2", "result": json.dumps(summary)})
    assert cli.main(["check", "--read", str(saved)]) == 1
    assert "Owners: user-b (1)." in capsys.readouterr().out
    summary["counts"]["elements"] = 3
    write(saved, summary)
    assert cli.main(["check", "--read", str(saved)]) == 2
    assert "Run read_ownership.py again" in capsys.readouterr().out


def test_summary_alone_never_passes_as_a_clean_check(tmp_path, capsys):
    cli = _cli()
    path = tmp_path / "own.json"
    write(path, {"document": "Project A", "workshared": True, "counts": {"elements": 2}})
    assert cli.main(["check", "--read", str(path)]) == 2
    assert "no \"elements\" list" in capsys.readouterr().out


def test_failed_reads_exit_2(tmp_path, capsys):
    cli = _cli()
    path = tmp_path / "own.json"
    write(path, {"status": "error", "run_id": "r3", "result": None, "error": "revit_busy"})
    assert cli.main(["check", "--read", str(path)]) == 2
    assert "revit_busy" in capsys.readouterr().out
    write(path, {"status": "bad_inputs", "error": "out_path must be an absolute path"})
    assert cli.main(["borrowed", "--read", str(path)]) == 2
    assert cli.main(["check", "--read", str(tmp_path / "missing.json")]) == 2


def test_inputs_prints_the_run_query_inputs_with_absolute_paths(tmp_path, capsys):
    cli = _cli()
    prev = tmp_path / "x.preview.json"
    write(prev, {"steps": [[7, "A1", "A2"]]})
    dry = tmp_path / "dry.json"
    write(dry, [{"type": "text", "text": json.dumps(
        {"status": "ok", "result": json.dumps({"modified_count": 1,
                                               "modified_by_category": {"Sheets": 1}})})}])
    out = tmp_path / "own-after-inputs.json"
    assert cli.main(["inputs", "--preview", str(prev), "--dryrun", str(dry), "--out", str(out)]) == 0
    printed = capsys.readouterr().out.strip().splitlines()
    call = json.loads(printed[-1])
    assert call == {"inputs_path": fwd(out), "out_path": fwd(tmp_path / "own-after.json")}
    assert json.load(io.open(str(out), encoding="utf-8"))["categories"] == ["Sheets"]
    assert cli.result_path(str(tmp_path / "ids.json")) == fwd(tmp_path / "ids-read.json")
    assert cli.result_path("x", str(tmp_path / "r.json")) == fwd(tmp_path / "r.json")


def test_round_trip_read_by_file_then_check(monkeypatch, tmp_path, capsys):
    """inputs -> read_ownership.py with the printed inputs -> check, no hand copy."""
    cli = _cli()
    prev = tmp_path / "x.preview.json"
    write(prev, {"steps": [[i, "a", "b"] for i in (101, 102, 103, 104, 105)]})
    inp = tmp_path / "own-inputs.json"
    assert cli.main(["inputs", "--preview", str(prev), "--out", str(inp)]) == 0
    call = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    summary = run_read(monkeypatch, model(), call)
    assert summary["counts"]["owned_by_others"] == 3
    assert cli.main(["check", "--read", call["out_path"], "--preview", str(prev)]) == 1
    out = capsys.readouterr().out
    assert "3 of 5 elements owned by others. Owners: Zoë (2), user-b (1)." in out
    assert json.load(io.open(str(prev), encoding="utf-8"))["blocked"] is True
