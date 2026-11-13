from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import read_batch_models as host


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "batch_model_audit.py"), *map(str, args)],
        capture_output=True, text=True,
    )


@pytest.fixture
def batch(tmp_path):
    root = tmp_path / "sources"
    root.mkdir()
    for name in ("E", "B", "A", "D", "C", "A.0001"):
        (root / (name + ".rvt")).write_bytes(("disposable fixture " + name).encode())
    workspace = tmp_path / "work"
    plan_path = tmp_path / "plan.json"
    result = run("prepare", root, "--workspace", workspace, "--revit", 2026)
    assert result.returncode == 0, result.stderr
    plan_path.write_text(result.stdout)
    return root, workspace, plan_path, json.loads(result.stdout)


def fake_host(monkeypatch, *, close_fails=False, workshared=False, partial=False,
              extra_document_at=None):
    events = []
    documents = []
    extra_document = object()

    class Document:
        IsFamilyDocument = IsWorkshared = IsModelInCloud = IsModified = False

        def __init__(self, path):
            self.PathName = path

        def Close(self, save):
            events.append(("close", save))
            if close_fails:
                return False
            documents.remove(self)
            return True

    def open_document(path, _options):
        events.append(("open", Path(path).name))
        doc = Document(path)
        documents.append(doc)
        if extra_document_at == "open":
            documents.append(extra_document)
        return doc

    app = SimpleNamespace(Documents=documents, VersionNumber="2026", VersionBuild="26.3.0.37",
                          OpenDocumentFile=open_document)
    ui = SimpleNamespace(Application=app, ActiveUIDocument=None)
    db = SimpleNamespace(
        BasicFileInfo=SimpleNamespace(Extract=lambda path: SimpleNamespace(
            IsWorkshared=workshared, Format="2026", Dispose=lambda: None)),
        ModelPathUtils=SimpleNamespace(ConvertUserVisiblePathToModelPath=lambda path: path),
        TransmissionData=SimpleNamespace(ReadTransmissionData=lambda _path: None),
        OpenOptions=object,
    )

    def audits(script, doc, running_app, inputs):
        assert doc in documents and running_app is app and inputs["max_ids"] == 1
        if b"am-model-health:" in script:
            events.append(("audit", "health"))
            if extra_document_at == "health":
                documents.append(extra_document)
            return {"metrics": [{"id": "warnings", "status": "warn"}],
                    "cad_view_count": {"partial": partial}}
        assert b"am-param-qa:" in script
        events.append(("audit", "parameter"))
        return {"summary": [{"total": 2, "passed": 1, "failed": 1}],
                "unknown_categories": [], "rows_truncated": True, "total_offenders": 1}

    # The fixture host models API objects. The native audit implementations are
    # present and pinned in staging, but only Revit can execute their API calls.
    real_model_result = host.model_result

    def model_result(app, db, path, model, year, scripts):
        return real_model_result(app, db, path, model, year, scripts, audit=audits)

    monkeypatch.setattr(host, "model_result", model_result)
    return ui, db, events


def collect(plan):
    return run("collect", "--plan", plan, "--host-exited")


def test_five_inputs_stage_and_collect_with_unchanged_hashes(batch, monkeypatch):
    root, workspace, plan_path, plan = batch
    ui, db, events = fake_host(monkeypatch)
    before = {file.name: sha256(file.read_bytes()).hexdigest() for file in root.iterdir()}
    manifest = json.loads((workspace / "host-manifest.json").read_text())
    assert str(root) not in (workspace / "host-manifest.json").read_text()
    assert [item["id"] for item in manifest["models"]] == [1, 2, 3, 4, 5]
    host.host_run(ui, db, "7.0.0.26278+2233", str(workspace))
    result = collect(plan_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert [row["model"] for row in report["models"]] == ["A.rvt", "B.rvt", "C.rvt", "D.rvt", "E.rvt"]
    assert all(row["original_unchanged"] and row["staged_unchanged"] for row in report["models"])
    assert all(row["summary"]["parameter_offenders"] == 1 for row in report["models"])
    assert all(row["summary"]["parameter_details_truncated"] for row in report["models"])
    assert events.count(("close", False)) == 5 and not ui.Application.Documents
    assert before == {file.name: sha256(file.read_bytes()).hexdigest() for file in root.iterdir()}
    assert plan["run_id"] == report["run_id"]


@pytest.mark.parametrize("mutant", ["missing", "stale", "duplicate", "wrong_digest"])
def test_invalid_receipts_keep_all_rows_and_no_audited_claim(batch, monkeypatch, mutant):
    _, work, plan, _ = batch
    ui, db, _ = fake_host(monkeypatch)
    if mutant != "missing":
        host.host_run(ui, db, "7.0.0.26278+2233", str(work))
        receipt = work / "host-receipt.json"
        data = json.loads(receipt.read_text())
        if mutant == "stale":
            data["run_id"] = "stale"
        elif mutant == "duplicate":
            data["rows"][0] = data["rows"][1]
        else:
            data["rows"][0]["sha256"] = "0" * 64
        receipt.write_text(json.dumps(data))
    result = collect(plan)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert [r["status"] for r in report["models"]] == ["receipt_invalid"] * 5


def test_source_or_copy_change_invalidates_the_row(batch, monkeypatch):
    root, work, plan, _ = batch
    ui, db, _ = fake_host(monkeypatch)
    host.host_run(ui, db, "7.0.0.26278+2233", str(work))
    source = root / "A.rvt"
    identical = root / "replacement"
    identical.write_bytes(source.read_bytes())
    identical.replace(source)
    (work / "model-002.rvt").write_bytes(b"changed")
    result = collect(plan)
    assert result.returncode == 1
    assert [row["status"] for row in json.loads(result.stdout)["models"]] == [
        "source_changed", "staged_changed", "audited", "audited", "audited",
    ]


def test_close_failure_stops_other_models(batch, monkeypatch):
    _, work, plan, _ = batch
    ui, db, events = fake_host(monkeypatch, close_fails=True)
    host.host_run(ui, db, "7.0.0.26278+2233", str(work))
    result = collect(plan)
    assert result.returncode == 1
    assert [row["status"] for row in json.loads(result.stdout)["models"]] == [
        "close_failed", "not_run", "not_run", "not_run", "not_run",
    ]
    assert events.count(("close", False)) == 1


@pytest.mark.parametrize("extra_document_at", ["open", "health"])
def test_extra_document_stops_audits_and_leaves_other_document_open(batch, monkeypatch, extra_document_at):
    _, work, plan, _ = batch
    ui, db, events = fake_host(monkeypatch, extra_document_at=extra_document_at)
    host.host_run(ui, db, "7.0.0.26278+2233", str(work))

    result = collect(plan)
    assert result.returncode == 1
    assert [row["status"] for row in json.loads(result.stdout)["models"]] == [
        "close_failed", "not_run", "not_run", "not_run", "not_run",
    ]
    expected_audits = [("audit", "health")] if extra_document_at == "health" else []
    assert [event for event in events if event[0] == "audit"] == expected_audits
    assert events.count(("close", False)) == 1
    assert len(ui.Application.Documents) == 1


def test_workshared_preflight_never_opens_a_model(batch, monkeypatch):
    _, work, plan, _ = batch
    ui, db, events = fake_host(monkeypatch, workshared=True)
    host.host_run(ui, db, "7.0.0.26278+2233", str(work))
    assert not events
    result = collect(plan)
    assert result.returncode == 1
    assert [row["status"] for row in json.loads(result.stdout)["models"]] == ["rejected"] * 5


def test_partial_metrics_are_distinct_from_audited(batch, monkeypatch):
    _, work, plan, _ = batch
    ui, db, _ = fake_host(monkeypatch, partial=True)
    host.host_run(ui, db, "7.0.0.26278+2233", str(work))
    result = collect(plan)
    assert result.returncode == 1
    assert [row["status"] for row in json.loads(result.stdout)["models"]] == ["incomplete"] * 5


def test_empty_folder_does_not_create_workspace(tmp_path):
    root, work = tmp_path / "empty", tmp_path / "work"
    root.mkdir()
    result = run("prepare", root, "--workspace", work, "--revit", 2026)
    assert result.returncode == 2 and not result.stdout and not work.exists()
