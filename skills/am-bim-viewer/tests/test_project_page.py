import base64
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
sys.path.insert(0, SCRIPTS)

import project_page as pp


AUDIT = {
    "source": "mep",
    "findings": [
        {"ruleId": "R1", "severity": "warning", "elementId": 1, "message": "w"},
        {"ruleId": "R2", "severity": "error", "elementId": 2, "message": "e"},
    ],
}
PREVIEW = {
    "title": "Shift",
    "columns": ["Id", "Num"],
    "changedColumns": ["Num"],
    "before": [[1, "A1"]],
    "after": [[1, "A2"]],
    "notes": ["1 of 1 change"],
}


def test_summary_counts():
    f = pp.collect_findings([AUDIT])
    s = pp.summarize(f)
    assert s["error"] == 1 and s["warning"] == 1 and s["total"] == 2


def test_errors_sort_first():
    html_out = pp.render_findings(pp.collect_findings([AUDIT]))
    assert html_out.index("sev-error") < html_out.index("sev-warning")


def test_model_text_is_escaped():
    bad = {"findings": [{"ruleId": "X", "severity": "error", "message": "<script>alert(1)</script>"}]}
    page = pp.build_page("T<b>", pp.collect_findings([bad]), {})
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "T<b>" not in page


def test_preview_shows_change_and_gate():
    out = pp.render_preview(PREVIEW)
    assert "<del>A1</del>" in out and "<ins>A2</ins>" in out
    assert "Keep/Discard" in out


def test_page_has_no_network_and_cli_works(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(json.dumps(AUDIT), encoding="utf-8")
    out = tmp_path / "p.html"
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--audit", str(a), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    page = out.read_text(encoding="utf-8")
    assert "http://" not in page and "https://" not in page
    assert "default-src 'none'" in page


def test_preview_pairs_reordered_rows_and_shows_additions_and_removals():
    preview = {
        "columns": ["Description", "ElementId"],
        "keyColumn": "ElementId",
        "changedColumns": ["Description"],
        "before": [["Old", "9007199254740993"], ["Retired", "2"], ["Same", "3"]],
        "after": [["Added <item>", "4"], ["Same", "3"], ["New", "9007199254740993"]],
    }
    out = pp.render_preview(preview)
    assert "<del>Old</del> &rarr; <ins>New</ins></td><td>9007199254740993</td>" in out
    assert "<td>Removed</td><td>Retired</td><td>2</td>" in out
    assert "<td>Added</td><td>Added &lt;item&gt;</td><td>4</td>" in out
    assert "<td>Unchanged</td><td>Same</td><td>3</td>" in out


def test_creation_only_preview_shows_rows_and_blocking_status():
    preview = {
        "columns": ["Id", "Name"],
        "keyColumn": "Id",
        "changedColumns": ["Name"],
        "before": [],
        "after": [["1", "New view"]],
        "blocked": True,
        "warnings": [{"text": "Name already used", "blocking": True}],
    }
    out = pp.render_preview(preview)
    assert "<td>Added</td><td>1</td><td>New view</td>" in out
    assert "Preview blocked" in out and "Name already used" in out
    assert "Approve or discard" not in out


def test_project_page_preserves_review_only_wording_preview_and_wrapper(tmp_path):
    root = Path(HERE).parents[2]
    notes = tmp_path / "notes.json"
    notes.write_text(json.dumps({"notes": [{"id": 15, "text": "Dorr <tag>", "view": "Plan"}]}))
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"accepted": ["Door", "tag"], "replacements": {"Dorr": "Door"}}))
    payload = json.loads(subprocess.check_output([
        sys.executable, root / "skills/am-shared-text-check/scripts/shared_text_cli.py",
        "wording-preview", notes, rules,
    ], text=True))
    path = tmp_path / "preview.json"
    out = tmp_path / "project.html"
    command = [sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--preview", path, "--out", out]
    wrapped = {"preview": payload, "plan": {"private": "plan-sentinel"}}
    for document in (payload, wrapped, {"status": "ok", "result": {"payload": json.dumps(wrapped)}}):
        path.write_text(json.dumps(document))
        completed = subprocess.run(command, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr
        page = out.read_text()
        assert "<del>Dorr &lt;tag&gt;</del>" in page and "<ins>Door &lt;tag&gt;</ins>" in page
        assert "Review only" in page and "Approve or discard" not in page
        assert "plan-sentinel" not in page
    path.write_text(json.dumps({"status": "ok", "result_truncated": True, "result": payload}))
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 1 and "complete change-preview" in completed.stderr
    assert out.read_text() == page


def test_project_page_renders_parsed_issue_board(tmp_path):
    root = Path(HERE).parents[2]
    note = tmp_path / "note.json"
    note.write_text(json.dumps({"text": "\r".join([
        "Coordination board",
        "Issues use fixed numbers; next new issue is #2. Never renumber.",
        "#1 [OPEN] Check <img src=x> - @reviewer",
        "Severity: High",
        "Finding: Check opening id9007199254740993.",
        "To do: Review the opening.",
        "2026-11-11 @reviewer: Keep this on the board.",
        "Related: #42",
    ])}), encoding="utf-8")
    board = tmp_path / "board.json"
    board.write_bytes(subprocess.check_output([
        sys.executable, root / "skills/am-issue-board/scripts/board_cli.py", "parse", note,
    ]))
    out = tmp_path / "project.html"
    result = subprocess.run([
        sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--board", board, "--out", out,
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    page = out.read_text()
    assert "<h2>Issue board</h2>" in page and "Coordination board" in page
    assert "<summary>#1 [OPEN] Check &lt;img src=x&gt;</summary>" in page
    assert "<img" not in page
    assert "id9007199254740993" in page and "@reviewer" in page and "High" in page
    assert "Review the opening." in page and "Keep this on the board." in page and "#42" in page
    assert "default-src 'none'" in page


def test_issue_board_filters_use_exact_unambiguous_model_ids():
    identifier = 9007199254740993
    model = {"elements": [
        {"id": str(identifier), "category": "<Walls>", "level": "Level 1"},
        {"id": identifier - 1, "category": "Adjacent ID", "level": "Level 2"},
        {"id": 3, "category": "Conflicting ID", "level": "Unknown"},
        {"id": "3", "category": "Other duplicate", "level": "Unknown"},
    ]}
    issue = {"number": 1, "status": "OPEN", "title": "Check", "severity": ["High"],
             "element_ids": [identifier, 3], "findings": [], "todos": [], "assignees": [],
             "comments": [], "related": []}
    board = {"header": {"title": "Board"}, "warnings": [], "issues": [issue]}
    page = pp.build_page("Project", [], {}, board=board, viewer_name="model.viewer.html",
                         model_facets=pp.model_issue_facets(model))

    class Parser(HTMLParser):
        facets = None

        def handle_starttag(self, tag, attributes):
            if tag == "details":
                self.facets = json.loads(dict(attributes)["data-facets"])

    parser = Parser()
    parser.feed(page)
    assert parser.facets == {"severity": ["High"], "elements": [{"category": "<Walls>", "level": "Level 1"}]}
    assert "<option value='&lt;Walls&gt;'>&lt;Walls&gt;</option>" in page
    assert all(text not in page for text in ("<Walls>", "Adjacent ID", "Conflicting ID", "Other duplicate"))
    assert "name='level'" in page and "name='severity'" in page
    assert "href='model.viewer.html#elements=9007199254740993,3'>Frame 1 of 2 issue elements" in page
    assert "name='category'" not in pp.build_page("Project", [], {}, board=board)
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in page


def test_invalid_board_export_does_not_replace_project_page(tmp_path):
    board = tmp_path / "board.json"
    board.write_text(json.dumps({"findings": []}))
    out = tmp_path / "project.html"
    out.write_text("Existing project page")
    result = subprocess.run([
        sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--board", board, "--out", out,
    ], capture_output=True, text=True)
    assert result.returncode == 1 and "board_cli.py parse" in result.stderr
    assert out.read_text() == "Existing project page"


def test_model_health_snapshot_flags_its_bad_and_warn_metrics(tmp_path):
    import runpy

    root = Path(HERE).parents[2]
    metric = runpy.run_path(str(root / "skills/am-model-health/scripts/model_health.py"))["metric"]
    snapshot = {
        "model": "Coordination <img src=x>",
        "revit_year": "2026",
        "timestamp": "2026-11-11T12:00:00",
        "path": "/private/model-file.rvt",
        "metrics": [
            metric("file_size", "File size", 712, 500, 700, "Review imported CAD.", unit="MB"),
            metric("links_unpinned", "Unpinned links", 1, 0, 3, "Check the link."),
            metric("rooms_unplaced", "Rooms not placed", 0, 0, 20, "Review rooms."),
            metric("links_total", "Link instances", 2, None, None, "Inventory."),
        ],
    }
    audit = tmp_path / "health.json"
    audit.write_text(json.dumps(snapshot), encoding="utf-8")
    out = tmp_path / "project.html"
    result = subprocess.run([
        sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--audit", audit, "--out", out,
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    page = out.read_text()
    assert "<td>error</td><td>file_size</td>" in page and "File size (712 MB)" in page
    assert "<td>warning</td><td>links_unpinned</td>" in page and "Unpinned links (1)" in page
    assert "2026-11-11T12:00:00" in page and "Coordination &lt;img src=x&gt;" in page
    assert "<img" not in page and snapshot["path"] not in page
    assert "rooms_unplaced" not in page and "links_total" not in page


def test_failed_health_audit_cannot_be_rendered_as_clean(tmp_path):
    audit = tmp_path / "health.json"
    audit.write_text(json.dumps({"error": "no_project_document"}))
    out = tmp_path / "project.html"
    out.write_text("Existing project page")
    result = subprocess.run([
        sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--audit", audit, "--out", out,
    ], capture_output=True, text=True)
    assert result.returncode == 1
    assert "audit" in result.stderr.lower()
    assert out.read_text() == "Existing project page"


def test_project_page_reads_completed_audit_envelopes_and_reassembled_preview(tmp_path):
    audit = tmp_path / "audit.json"
    preview = tmp_path / "preview.json"
    out = tmp_path / "project.html"
    complete_audit = {"mode": "query", "decision": "rolled_back", "status": "ok", "exit_code": 0,
                      "result_truncated": False, "result": AUDIT,
                      "changes": {"added_count": 0, "modified_count": 0, "deleted_count": 0}}
    audit.write_text(json.dumps({"response": complete_audit}))
    raw = json.dumps({"preview": dict(PREVIEW, title="Preview \U0001f4d0"), "plan": {"hidden": "plan-sentinel"}},
                     ensure_ascii=False).encode("utf-16-le")
    # A Get Run page can end between the two UTF-16 code units of a non-BMP character.
    split = raw.index("\U0001f4d0".encode("utf-16-le")) + 2
    response = {"mode": "query", "decision": "rolled_back", "status": "ok", "run_id": "preview-run",
                "result": None, "result_truncated": True}
    pages = []
    for offset, chunk in ((split, raw[split:]), (0, raw[:split])):
        pages.append(dict(response, result_page={"offset": offset // 2, "length": len(chunk) // 2,
                                                  "total_length": len(raw) // 2,
                                                  "text": chunk.decode("utf-16-le", errors="surrogatepass")}))
    preview.write_text(json.dumps(pages))
    command = [sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--audit", audit, "--preview", preview, "--out", out]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    generated = out.read_text()
    assert "<td>R2</td>" in generated and "Preview \U0001f4d0" in generated
    assert "<del>A1</del>" in generated and "plan-sentinel" not in generated

    bad_pages = [pages[:1], [pages[0], dict(pages[1], run_id="other-run")], [*pages, pages[0]]]
    for bad in bad_pages:
        preview.write_text(json.dumps(bad))
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode == 1 and "complete change-preview" in result.stderr
        assert out.read_text() == generated
    preview.write_text(json.dumps(pages))
    for bad in (dict(complete_audit, status="failed"), dict(complete_audit, decision="committed"),
                dict(complete_audit, changes={"modified_count": 1})):
        audit.write_text(json.dumps(bad))
        result = subprocess.run(command, capture_output=True, text=True)
        assert result.returncode == 1 and "complete read-only audit" in result.stderr
        assert out.read_text() == generated


def test_project_page_lists_clean_model_health_runs_newest_first(tmp_path):
    older = tmp_path / "old.json"
    newer = tmp_path / "new.json"
    clean = {"model": "Coordination <East>", "timestamp": "2026-11-11T12:00:00",
             "metrics": [{"id": "links", "label": "Links", "status": "ok",
                          "value": 1, "unit": "", "hint": ""}]}
    older.write_text(json.dumps(clean))
    newer.write_text(json.dumps({**clean, "timestamp": "2026-11-11T15:00:00"}))
    out = tmp_path / "project.html"
    result = subprocess.run([
        sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--audit", older,
        "--audit", newer, "--out", out,
    ], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    page = out.read_text()
    assert "<h2>Model health audits</h2>" in page
    assert "<p>No findings.</p>" in page
    assert page.index("2026-11-11T15:00:00") < page.index("2026-11-11T12:00:00")
    assert "<th scope='row'>Coordination &lt;East&gt;</th><td>2026-11-11T15:00:00</td><td>1</td><td>0</td><td>0</td>" in page


def test_project_page_builds_and_links_local_model(tmp_path, monkeypatch):
    model = tmp_path / "model.json"
    model.write_text('{"elements": []}')
    out = tmp_path / "project.html"
    viewer = b"<!doctype html><title>Offline BIM viewer</title>"

    def build_model(command, **kwargs):
        assert command[0] == "node"
        assert Path(command[1]).name == "build-viewer.mjs"
        assert command[2] == "--offline"
        assert Path(command[3]) == model.resolve()
        Path(command[4]).write_bytes(viewer)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", build_model)
    monkeypatch.setattr(sys, "argv", ["project_page.py", "--model", str(model), "--out", str(out)])
    assert pp.main() == 0
    sibling = f"model-{hashlib.sha256(viewer).hexdigest()}.viewer.html"
    assert (tmp_path / sibling).read_bytes() == viewer
    assert f"href='{sibling}'" in out.read_text()
    assert str(model) not in out.read_text()
    assert model.read_text() == '{"elements": []}'


def test_project_model_preserves_numeric_revit_ids_beyond_javascript_precision(tmp_path, monkeypatch):
    model = tmp_path / "model.json"
    identifier = 9007199254740993
    original = {"elements": [{"id": identifier, "category": "Walls"}, {"id": 1}]}
    model.write_text(json.dumps(original))
    issue = {"number": 1, "status": "OPEN", "title": "Check", "severity": [], "element_ids": [identifier],
             "findings": [], "todos": [], "assignees": [], "comments": [], "related": []}
    board = {"header": {"title": "Board"}, "warnings": [], "issues": [issue]}
    out = tmp_path / "project.html"

    def build_model(command, **kwargs):
        source = Path(command[3])
        assert source != model
        assert json.loads(source.read_text())["elements"] == [
            {"id": str(identifier), "category": "Walls"}, {"id": 1},
        ]
        Path(command[4]).write_text("<!doctype html><title>Viewer</title>")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", build_model)
    pp.write_project_with_model(out, model, "Project", [], {}, board, [])
    assert f"#elements={identifier}" in out.read_text()
    assert json.loads(model.read_text()) == original


def test_project_page_preserves_old_report_if_model_build_fails(tmp_path, monkeypatch):
    model = tmp_path / "model.json"
    model.write_text("invalid JSON")
    out = tmp_path / "project.html"
    out.write_text("Existing project page")

    def fail_model(command, **kwargs):
        Path(command[4]).write_text("partial viewer")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", fail_model)
    monkeypatch.setattr(sys, "argv", ["project_page.py", "--model", str(model), "--out", str(out)])
    assert pp.main() == 1
    assert out.read_text() == "Existing project page"
    assert list(tmp_path.glob("*.viewer.html")) == []
