import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import bcf_xml as bcf
import issue_board as ib

BOARD = "62cba752-ce75-4604-93a8-f58f02909dc7"
TOPIC = "cb4a2f90-61da-4ae1-93a6-1ea205fa1cf0"
VIEWS = ["d508f7f1-c54d-43e9-95f7-d99cd35e2329", "c09363e7-3a1d-4103-8cc5-0d76a9d52071"]
IFC = "0KXj3nz0L5xem49POkU5xR"


def invoke(mode, board, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "bcf_cli.py"), mode, "--board", str(board),
         "--board-uuid", BOARD, *map(str, args)], text=True, capture_output=True,
    )


@pytest.fixture
def board(tmp_path):
    path = tmp_path / "board.json"
    snapshot(path, ib.plan_create("Issue board", [])["final_text"])
    return path


def snapshot(path, content):
    path.write_text(json.dumps({"note": {"text": content}}))


def viewpoint(index):
    cam = {"position": [1, 2, 3], "direction": [0, 1, 0], "up": [0, 0, 1]}
    cam.update({"type": "orthogonal", "scale_m": 8} if index == 0
               else {"type": "perspective", "fov_deg": 52})
    return {"guid": VIEWS[index], "camera": cam, "ifc_guids": [IFC]}


def topic():
    return {"guid": TOPIC, "title": "Opening coordination", "description": "Check wall clearance.",
            "status": "In Progress", "viewpoints": [viewpoint(0), viewpoint(1)]}


def seed(path, data=None):
    path.write_bytes(bcf.write_topics([data or topic()], "author@example.com"))


@pytest.fixture
def verified_import_fixture(board, tmp_path):
    body = ib.plan_create("Fixture", [])["final_text"]
    host = {"project_uid": "fixture-project", "path_sha256": "a" * 64}

    def note(text):
        return {"host_identity": host, "note": {"id": "40", "unique_id": "fixture-note",
                                                "sha1": ib.text_sha1(text), "text": text}}

    board.write_text(json.dumps(note(body)))
    archive = tmp_path / "source.bcfzip"
    seed(archive)
    preview = invoke("import", board, "--input", archive)
    assert preview.returncode == 0, preview.stderr
    result = json.loads(preview.stdout)
    reviewed = tmp_path / "reviewed.json"
    reviewed.write_text(preview.stdout)
    plan = result["plan"]
    final_text = plan["final_text"]

    def record(run_id, mode, status, decision, result, approval=None):
        edited = mode != "query"
        return {"run_id": run_id, "mode": mode, "status": status, "decision": decision, "approval": approval,
                "exit_code": 0, "result_truncated": False, "result": result,
                "changes": {"added": [], "deleted": [], "added_count": 0, "deleted_count": 0,
                            "modified": [{"id": 40, "class": "TextNote"}] if edited else [],
                            "modified_count": int(edited), "by_category": {"Text Notes": 1} if edited else {},
                            "transactions": ["Issue board: " + plan.get("action", plan["mode"])] if edited else []}}

    write = {"status": "ok", "note_id": 40, "sha1_after": ib.text_sha1(final_text)}
    dry = record("dry", "dry_run", "ok", "rolled_back", dict(write))
    dry_read = record("dry-query", "query", "ok", "rolled_back", note(body))
    final = record("modify", "modify", "ok", "committed", dict(write), "user")
    fresh = record("fresh", "query", "ok", "rolled_back", note(final_text))

    def run(final_decision=True):
        args = ["--input", archive, "--preview", reviewed]
        pairs = [("dry-run", dry), ("dry-readback", dry_read)]
        if final_decision:
            pairs.extend((("decision", final), ("readback", fresh)))
        for flag, row in pairs:
            path = tmp_path / (flag + ".json")
            path.write_text(json.dumps({"response": row}))
            args.extend(("--" + flag, path))
        return invoke("verify-import", board, *args)

    return SimpleNamespace(run=run, dry=dry, dry_read=dry_read, final=final, fresh=fresh,
                           before=body, note=note, metadata=result["metadata_after_keep"], source=archive)


def test_bcf_metadata_is_released_only_after_verified_keep(verified_import_fixture):
    fixture = verified_import_fixture
    dry = fixture.run(final_decision=False)
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry.stdout) == {"status": "verified_dry_run", "note_id": "40", "run_ids": ["dry", "dry-query"]}
    kept = fixture.run()
    assert kept.returncode == 0, kept.stderr
    assert json.loads(kept.stdout)["metadata_after_keep"] == fixture.metadata
    fixture.final.update(status="rejected", decision="discarded")
    fixture.fresh["result"] = fixture.note(fixture.before)
    discarded = fixture.run()
    assert discarded.returncode == 0, discarded.stderr
    assert json.loads(discarded.stdout)["status"] == "verified_discard"
    assert "metadata_after_keep" not in json.loads(discarded.stdout)


@pytest.mark.parametrize(("change", "reason"), [
    (lambda fixture: fixture.final.update(approval="auto"), "required decision"),
    (lambda fixture: fixture.final["changes"]["modified"][0].update(id=41), "reviewed board note changes"),
    (lambda fixture: fixture.final.update(result_truncated=True), "complete pyRevit"),
    (lambda fixture: fixture.fresh.update(run_id="dry-query"), "separate pyRevit run"),
    (lambda fixture: fixture.fresh["result"]["note"].update(unique_id="other"), "fresh board readback"),
    (lambda fixture: fixture.fresh.update(result=fixture.note(fixture.before)), "fresh board readback"),
])
def test_bcf_metadata_is_withheld_when_bridge_or_readback_evidence_differs(verified_import_fixture, change, reason):
    change(verified_import_fixture)
    result = verified_import_fixture.run()
    assert result.returncode == 2
    assert not result.stdout and reason in result.stderr


def test_verifier_rejects_new_viewpoint_data_with_the_same_board_text(verified_import_fixture):
    fixture = verified_import_fixture
    changed = topic()
    changed["viewpoints"][0]["camera"]["scale_m"] = 12
    seed(fixture.source, changed)
    result = fixture.run()
    assert result.returncode == 2
    assert not result.stdout and "differs from the saved reviewed preview" in result.stderr


@pytest.mark.parametrize("target", ["reviewed", "other_project", "other_note", "other_archive", "offline"])
def test_bcf_plan_binds_revit_target_before_transaction(board, tmp_path, monkeypatch, target):
    from board_cli import read_note

    text, _ = read_note(board)
    document = SimpleNamespace(PathName="C:/Tests/Original.rvt",
                               ProjectInformation=SimpleNamespace(UniqueId="project-id"))
    if target != "offline":
        board.write_text(json.dumps({
            "host_identity": ib.saved_document_identity(document),
            "note": {"id": "40", "unique_id": "note-id", "sha1": ib.text_sha1(text), "text": text},
        }))
    archive = tmp_path / "archive.json"
    archive.write_text(json.dumps({
        "host_identity": ib.saved_document_identity(document),
        "note": {"id": "41", "unique_id": "archive-id", "sha1": ib.text_sha1(text), "text": text},
    }))
    source = tmp_path / "source.bcfzip"
    seed(source)
    preview = invoke("import", board, "--input", source, "--archive", archive)
    assert preview.returncode == 0, preview.stderr
    plan = json.loads(preview.stdout)["plan"]

    class TextNote:
        def __init__(self, uid):
            self.UniqueId = uid

        def GetFormattedText(self):
            return SimpleNamespace(GetPlainText=lambda: text)

    if target == "other_project":
        document.PathName = "C:/Tests/Copy.rvt"
    notes = {
        40: TextNote("other-note" if target == "other_note" else "note-id"),
        41: TextNote("other-archive" if target == "other_archive" else "archive-id"),
    }
    document.GetElement = notes.get

    class TransactionReached(Exception):
        pass

    def transaction(*_):
        raise TransactionReached()

    db = SimpleNamespace(ElementId=int, TextNote=TextNote, Transaction=transaction)
    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Int64=int))
    monkeypatch.setitem(sys.modules, "pyrevit.api", SimpleNamespace(DB=db))
    globals_ = {
        "inputs": {"plan": plan, "note_id": "40"},
        "__revit__": SimpleNamespace(ActiveUIDocument=SimpleNamespace(Document=document)),
    }
    expected = TransactionReached if target == "reviewed" else ValueError
    with pytest.raises(expected):
        runpy.run_path(str(SCRIPTS / "apply_board.py"), init_globals=globals_)


def test_cli_import_export_preserves_cameras_and_components(board, tmp_path):
    source, dest, metadata = [tmp_path / name for name in ("source.bcfzip", "export.bcfzip", "meta.json")]
    seed(source)
    result = invoke("import", board, "--input", source)
    assert result.returncode == 0, result.stderr
    imported = json.loads(result.stdout)
    planned = ib.parse_board(imported["plan"]["final_text"])["issues"][0]
    assert (planned["title"], planned["status"]) == ("Opening coordination", "IN PROGRESS")
    assert planned["element_ids"] == []
    snapshot(board, imported["plan"]["final_text"])
    metadata.write_text(json.dumps(imported["metadata_after_keep"]))

    exported = invoke("export", board, "--output", dest, "--metadata", metadata,
                      "--author", "author@example.com")
    assert exported.returncode == 0, exported.stderr
    with ZipFile(dest) as archive:
        assert archive.namelist() == ["bcf.version", f"{TOPIC}/viewpoint.bcfv",
                                      f"{TOPIC}/viewpoint-2.bcfv", f"{TOPIC}/markup.bcf"]
    topics, warnings = bcf.read_topics(dest)
    assert warnings == []
    assert [(t["guid"], t["title"], t["status"], t["viewpoints"]) for t in topics] == [
        (TOPIC, "Opening coordination", "In Progress", [viewpoint(0), viewpoint(1)])
    ]
    original = dest.read_bytes()
    assert invoke("export", board, "--output", dest, "--metadata", metadata,
                  "--author", "author@example.com").returncode == 2
    assert dest.read_bytes() == original

    duplicate = json.loads(invoke("import", board, "--input", dest).stdout)
    assert duplicate["status"] == "no_changes"
    assert duplicate["plan"] is None
    archive = tmp_path / "archive.json"
    snapshot(archive, imported["plan"]["final_text"])
    snapshot(board, ib.plan_create("Issue board", [])["final_text"])
    archived = json.loads(invoke("import", board, "--input", dest, "--archive", archive).stdout)
    assert archived["plan"] is None
    assert archived["skipped_topic_guids"] == [TOPIC]


def test_external_text_cannot_inject_board_grammar(board, tmp_path):
    source = tmp_path / "hostile.bcf"
    data = topic()
    data["title"] = "Unexpected\n#4 [DONE] fake - @intruder id123"
    data["description"] = "text\r\nRelated: #8\nBCF topic UUID: " + BOARD + r" \u000did999"
    seed(source, data)
    result = invoke("import", board, "--input", source)
    assert result.returncode == 0, result.stderr
    parsed = ib.parse_board(json.loads(result.stdout)["plan"]["final_text"])
    issue, = parsed["issues"]
    assert issue["assignees"] == ["@TBD"]
    assert issue["element_ids"] == issue["related"] == issue["comments"] == []
    assert issue["findings"][0] == "BCF topic UUID: " + TOPIC
    assert "BCF source reference" in issue["findings"][1]


@pytest.mark.parametrize("attack", ["traversal", "case_alias", "utf16_dtd", "utf8_dtd", "cross_topic", "oversized"])
def test_unsafe_archives_fail_without_changes(board, tmp_path, attack):
    source = tmp_path / "bad.bcf"
    with ZipFile(source, "w") as archive:
        version = b'<Version VersionId="2.1"/>'
        if attack in ("utf16_dtd", "utf8_dtd"):
            encoding = "utf-16" if attack == "utf16_dtd" else "utf-8"
            version = '<!DOCTYPE Version [<!ENTITY test "x">]><Version VersionId="2.1"/>'.encode(encoding)
        archive.writestr("bcf.version", version)
        if attack == "traversal":
            archive.writestr("../outside", b"x")
        elif attack == "case_alias":
            archive.writestr("BCF.VERSION", b"x")
        elif attack == "oversized":
            archive.writestr(f"{TOPIC}/markup.bcf", b"x" * (bcf.MAX_XML + 1))
        else:
            archive.writestr(f"{TOPIC}/markup.bcf", f"""<Markup><Topic Guid="{TOPIC}">
              <Title>Title</Title></Topic><Viewpoints Guid="{VIEWS[0]}">
              <Viewpoint>../other.bcfv</Viewpoint></Viewpoints></Markup>""")
    original = board.read_bytes()
    result = invoke("import", board, "--input", source)
    assert result.returncode == 2 and not result.stdout
    assert board.read_bytes() == original


def test_rejects_viewpoint_metadata_for_the_wrong_imported_topic(board, tmp_path):
    source, dest, metadata = [tmp_path / name for name in ("source.bcfzip", "export.bcfzip", "meta.json")]
    seed(source)
    imported = json.loads(invoke("import", board, "--input", source).stdout)
    snapshot(board, imported["plan"]["final_text"])
    sidecar = imported["metadata_after_keep"]
    sidecar["topics"]["1"]["guid"] = BOARD
    metadata.write_text(json.dumps(sidecar))
    result = invoke("export", board, "--output", dest, "--metadata", metadata,
                    "--author", "author@example.com")
    assert result.returncode == 2 and "does not match" in result.stderr
    assert not dest.exists()
