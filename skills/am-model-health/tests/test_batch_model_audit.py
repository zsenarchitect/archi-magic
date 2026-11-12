from hashlib import sha256
import json
import os
import runpy
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


def fake_host(monkeypatch, *, close_fails=False, workshared=False, partial=False, seeded=False):
    events = []
    documents = []

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
        number = int(Path(doc.PathName).stem.split("-")[-1])
        warnings = 60 * number if seeded and number < 3 else 60 * int(not seeded)
        offenders = int(not seeded or number in (3, 4))
        if b"am-model-health:" in script:
            events.append(("audit", "health"))
            return {"metrics": [{"id": "warnings", "status": "warn" if warnings else "ok", "value": warnings,
                                 "label": "Private model label", "details": [{"ids": [9]}]}],
                    "cad_view_count": {"partial": partial}}
        assert b"am-param-qa:" in script
        events.append(("audit", "parameter"))
        category = "OST_Rooms" if seeded and number == 4 else "OST_Doors"
        name = "Department" if category == "OST_Rooms" else "Mark"
        return {"summary": [{"key": category, "total": 2, "passed": 2 - offenders, "failed": offenders,
                             "by_param": {name: {"missing": 0, "empty": offenders, "on_type": 0}},
                             "category": "Private model category label", "offender_ids": [18]}],
                "unknown_categories": [], "rows_truncated": bool(offenders), "total_offenders": offenders}

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


def test_combined_report_keeps_exact_seeded_checks_and_clean_control(batch, monkeypatch):
    _, workspace, plan_path, _ = batch
    ui, db, _ = fake_host(monkeypatch, seeded=True)
    host.host_run(ui, db, "7.0.1.26312+1155", str(workspace))
    result = collect(plan_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    summaries = [row["summary"] for row in report["models"]]
    assert [row["health_checks"] for row in summaries] == [
        {"warnings": {"status": status, "value": count}}
        for status, count in [("warn", 60), ("warn", 120), ("ok", 0), ("ok", 0), ("ok", 0)]
    ]
    assert [row["parameter_checks"] for row in summaries] == [
        {category: {"total": 2, "passed": 2 - failed, "failed": failed,
                    "by_parameter": {name: {"missing": 0, "empty": failed, "on_type": 0}}}}
        for category, name, failed in [
            ("OST_Doors", "Mark", 0), ("OST_Doors", "Mark", 0), ("OST_Doors", "Mark", 1),
            ("OST_Rooms", "Department", 1), ("OST_Doors", "Mark", 0),
        ]
    ]
    assert "Private model" not in result.stdout
    assert "offender_ids" not in result.stdout and "details" not in result.stdout.replace("parameter_details_truncated", "")


def test_fixture_qualification_rejects_same_total_with_seed_on_wrong_model(batch, monkeypatch):
    root, workspace, plan_path, _ = batch
    ui, db, _ = fake_host(monkeypatch, seeded=True)
    host.host_run(ui, db, "7.0.1.26312+1155", str(workspace))
    expectations = root.parent / "expected.json"
    seeds = [
        {"id": number, "clean": number == 5, "health_checks": {}, "parameter_checks": {}}
        for number in range(1, 6)
    ]
    seeds[0]["health_checks"] = {"warnings": {"status": "warn", "value": 60}}
    seeds[1]["health_checks"] = {"warnings": {"status": "warn", "value": 120}}
    for index, category, name in [(2, "OST_Doors", "Mark"), (3, "OST_Rooms", "Department")]:
        seeds[index]["parameter_checks"] = {
            category: {"total": 2, "passed": 1, "failed": 1,
                       "by_parameter": {name: {"missing": 0, "empty": 1, "on_type": 0}}},
        }

    def compare():
        expectations.write_text(json.dumps({"models": seeds}))
        return run("collect", "--plan", plan_path, "--host-exited", "--fixture-expectations", expectations)

    verified = compare()
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["fixture_checks_matched"] is True
    seeds[0]["health_checks"], seeds[1]["health_checks"] = seeds[1]["health_checks"], seeds[0]["health_checks"]
    mismatch = compare()
    assert mismatch.returncode == 2
    assert "Model 1 did not match its seeded health_checks" in mismatch.stderr


@pytest.mark.parametrize("loaded_build", ["7.0.1.26312+1155", None])
def test_staged_worker_reads_version_from_loaded_runtime(batch, monkeypatch, loaded_build):
    _, work, _, _ = batch
    ui, db, events = fake_host(monkeypatch, workshared=True)
    executor = object()
    assembly = object()

    def get_clr_type(script_executor):
        assert script_executor is executor
        return SimpleNamespace(Assembly=assembly)

    def read_build(loaded_assembly):
        assert loaded_assembly is assembly
        return loaded_build

    monkeypatch.setitem(sys.modules, "clr", SimpleNamespace(GetClrType=get_clr_type))
    monkeypatch.setitem(sys.modules, "pyRevitLabs.Common", SimpleNamespace(
        PyRevitBuildVersion=SimpleNamespace(FromAssembly=read_build)))
    monkeypatch.setitem(sys.modules, "PyRevitLabs.PyRevit.Runtime", SimpleNamespace(ScriptExecutor=executor))
    monkeypatch.setitem(sys.modules, "pyrevit", SimpleNamespace(versionmgr=SimpleNamespace(
        get_loaded_build_version=lambda: "7.0.0.26278+2233")))
    monkeypatch.setitem(sys.modules, "pyrevit.api", SimpleNamespace(DB=db))

    def execute():
        runpy.run_path(str(work / "read_batch_models.py"), run_name="__main__",
                       init_globals={"__batchexec__": True, "__revit__": ui})

    receipt = work / "host-receipt.json"
    if loaded_build is None:
        with pytest.raises(ValueError, match="loaded pyRevit build"):
            execute()
        assert not receipt.exists()
    else:
        execute()
        assert json.loads(receipt.read_text())["runtime"]["pyrevit_build"] == loaded_build
    assert events == []


@pytest.mark.parametrize("mutant", [
    "missing", "stale", "duplicate", "wrong_digest", "unknown_build", "wrong_health_total", "wrong_parameter_total",
])
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
        elif mutant == "unknown_build":
            data["runtime"]["pyrevit_build"] = "Unknown"
        elif mutant == "wrong_health_total":
            data["rows"][0]["summary"]["health_checks"]["warnings"]["status"] = "bad"
        elif mutant == "wrong_parameter_total":
            data["rows"][0]["summary"]["parameter_checks"]["OST_Doors"]["by_parameter"]["Mark"]["empty"] = 2
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


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_host_refuses_staged_alias_before_opening(batch, monkeypatch, alias):
    root, work, plan, _ = batch
    source = root / "A.rvt"
    staged = work / "model-001.rvt"
    staged.unlink()
    if alias == "symlink":
        staged.symlink_to(source)
    else:
        os.link(source, staged)
    ui, db, events = fake_host(monkeypatch)
    host.host_run(ui, db, "7.0.0.26278+2233", str(work))
    receipt = json.loads((work / "host-receipt.json").read_text())
    assert [row["status"] for row in receipt["rows"]] == ["staged_changed"] + ["audited"] * 4
    assert ("open", "model-001.rvt") not in events
    result = collect(plan)
    assert result.returncode == 1
    assert len(json.loads(result.stdout)["models"]) == 5


def test_host_refuses_identical_staged_replacement_after_metadata_preflight(batch, monkeypatch):
    _, work, _, _ = batch
    staged = work / "model-001.rvt"
    replacement = work / "replacement"
    replacement.write_bytes(staged.read_bytes())
    ui, db, events = fake_host(monkeypatch)
    original_preflight = host.preflight

    def preflight(database, path, year):
        result = original_preflight(database, path, year)
        if Path(path) == staged:
            replacement.replace(staged)
        return result

    monkeypatch.setattr(host, "preflight", preflight)
    host.host_run(ui, db, "7.0.0.26278+2233", str(work))
    receipt = json.loads((work / "host-receipt.json").read_text())
    assert [row["status"] for row in receipt["rows"]] == ["staged_changed"] + ["audited"] * 4
    assert ("open", "model-001.rvt") not in events


def test_qualification_launch_uses_only_the_pinned_worker(batch, tmp_path):
    import qualify_batch_host as launcher

    root, work, _, _ = batch
    executable = tmp_path / "pyrevit.exe"
    executable.write_bytes(b"fixture executable")
    command, directory, manifest = launcher.qualification_command(str(executable), str(work))
    assert command == [str(executable), "run", str(work / "read_batch_models.py"), "--revit=2026"]
    assert directory == work and str(root) not in json.dumps(manifest)
    worker = work / "read_batch_models.py"
    worker.write_bytes(b"changed worker")
    manifest["audit_sha256"][worker.name] = sha256(worker.read_bytes()).hexdigest()
    (work / "host-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="does not match the bundled worker"):
        launcher.qualification_command(str(executable), str(work))


def test_qualification_releases_real_bootstrap_only_after_assignment(tmp_path, monkeypatch):
    from contextlib import nullcontext
    import qualify_batch_host as launcher

    marker = tmp_path / "started"
    command = [sys.executable, "-c", "from pathlib import Path; Path('started').write_text('started')"]

    class TestJob:
        def session(self):
            return nullcontext()

        def contain(self, process):
            assert process.poll() is None and not marker.exists()
            self.process = process

        def active(self):
            return int(self.process.poll() is None)

        def terminate(self):
            self.process.kill()

    class RefusedJob(TestJob):
        def contain(self, process):
            super().contain(process)
            raise ValueError("Assignment refused.")

    with pytest.raises(ValueError, match="Assignment refused"):
        launcher.run_contained(command, tmp_path, 5, job=RefusedJob())
    assert not marker.exists()
    launcher.run_contained(command, tmp_path, 5, job=TestJob())
    assert marker.read_text() == "started"
    marker.unlink()

    monkeypatch.setattr(launcher, "LOG_LIMIT", 8)
    noisy = [sys.executable, "-c", "print('bounded stdout and stderr')"]
    with pytest.raises(ValueError, match="exceeded its output limit"):
        launcher.run_contained(noisy, tmp_path, 5, job=TestJob())


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows guard")
def test_qualification_command_refuses_other_platforms(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "qualify_batch_host.py"), "--pyrevit", str(tmp_path / "pyrevit.exe"),
         "--workspace", str(tmp_path), "--offline-fixture-host"],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert "requires Windows" in result.stderr
    assert not (tmp_path / "qualification-attempt").exists()
