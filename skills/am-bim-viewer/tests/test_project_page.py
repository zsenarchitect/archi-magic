import json
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


def test_project_page_renders_parsed_issue_board(tmp_path):
    from pathlib import Path

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
