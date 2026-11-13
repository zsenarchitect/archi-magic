import json
import os
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_page_has_no_network_and_cli_works(tmp_path, encoding):
    a = tmp_path / "a.json"
    a.write_text(json.dumps(AUDIT), encoding=encoding)
    preview = tmp_path / "preview.json"
    preview.write_text(json.dumps(PREVIEW), encoding=encoding)
    out = tmp_path / "p.html"
    r = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "project_page.py"), "--audit", str(a),
         "--preview", str(preview), "--out", str(out)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    page = out.read_text(encoding="utf-8")
    assert "http://" not in page and "https://" not in page
    assert "default-src 'none'" in page
    assert "<td>R2</td>" in page
    assert "<del>A1</del>" in page
