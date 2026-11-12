"""Local project page generator (issue #187 Phase 1).

Builds one self-contained, read-only HTML page from local JSON files:
- audit findings (model_health.py, mep_qa.py, dwg_cad_audit.py, check_packs.py output),
- optional change-preview JSON (am-safe-change),
- optional read-only issue board JSON (board_cli.py parse).

No network call. No data leaves the machine. The page cannot change a model.
Model changes stay behind the Revit Keep/Discard step.

Usage:
    python project_page.py --title "Project A" --audit a.json [--audit b.json] \
        [--preview p.json] [--board board.json] --out project.html
"""

from __future__ import annotations

import argparse
from collections import Counter
import html
import math
from itertools import zip_longest
import json
import os
import sys
from typing import Any, Dict, List

SEVERITIES = ("error", "warning", "info")


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def model_health_findings(audit):
    metrics = audit.get("metrics")
    if (not isinstance(metrics, list) or not metrics
            or any(not isinstance(audit.get(field), str) or not audit[field].strip()
                   for field in ("model", "timestamp"))):
        raise ValueError("Expected a completed model-health audit with metrics, model, and timestamp.")
    source = f"Model health · {audit['model']} · {audit['timestamp']}"
    levels = {"bad": "error", "warn": "warning"}
    for metric in metrics:
        if (not isinstance(metric, dict)
                or metric.get("status") not in ("bad", "warn", "ok", "info")
                or any(not isinstance(metric.get(field), str) for field in ("id", "label", "unit", "hint"))
                or type(metric.get("value")) not in (int, float)
                or (isinstance(metric["value"], float) and not math.isfinite(metric["value"]))):
            raise ValueError("The model-health audit contains an invalid metric.")
        severity = levels.get(metric["status"])
        if severity:
            amount = f"{metric['value']} {metric['unit']}".rstrip()
            yield {
                "severity": severity,
                "ruleId": metric["id"],
                "message": f"{metric['label']} ({amount}) {metric['hint']}".rstrip(),
                "source": source,
            }


def collect_findings(audits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge findings from several audit result files."""
    merged: List[Dict[str, Any]] = []
    for audit in audits:
        if not isinstance(audit, dict) or audit.get("error"):
            raise ValueError("An audit input did not contain a completed audit result.")
        if "metrics" in audit:
            merged.extend(model_health_findings(audit))
            continue
        if not isinstance(audit.get("findings"), list):
            raise ValueError("An audit input needs a findings list or a model-health result.")
        source = audit.get("drawing_name") or audit.get("source") or "audit"
        for f in audit["findings"]:
            if not isinstance(f, dict):
                raise ValueError("An audit finding needs to be a JSON object.")
            item = dict(f)
            item.setdefault("source", source)
            if item.get("severity") not in SEVERITIES:
                item["severity"] = "info"
            merged.append(item)
    return merged


def summarize(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s: 0 for s in SEVERITIES}
    for f in findings:
        counts[f["severity"]] += 1
    counts["total"] = len(findings)
    return counts


def _e(value: Any) -> str:
    """Escape any value for HTML. Model text is data, never markup."""
    return html.escape("" if value is None else str(value), quote=True)


def render_findings(findings: List[Dict[str, Any]]) -> str:
    if not findings:
        return "<p>No findings.</p>"
    order = {s: i for i, s in enumerate(SEVERITIES)}
    rows = []
    for f in sorted(findings, key=lambda x: (order[x["severity"]], str(x.get("ruleId", "")))):
        target = f.get("target") or f.get("elementId") or ""
        rows.append(
            "<tr class='sev-{sev}'><td>{sev}</td><td>{rule}</td><td>{target}</td>"
            "<td>{msg}</td><td>{src}</td></tr>".format(
                sev=_e(f["severity"]), rule=_e(f.get("ruleId")), target=_e(target),
                msg=_e(f.get("message")), src=_e(f.get("source")),
            )
        )
    return (
        "<table><thead><tr><th>Severity</th><th>Rule</th><th>Target</th>"
        "<th>Message</th><th>Source</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def pair_preview_rows(preview):
    before = preview.get("before", [])
    after = preview.get("after", [])
    columns = preview.get("columns", [])
    key_column = preview.get("keyColumn")
    if key_column is None:
        return zip_longest(before, after)
    if key_column not in columns:
        raise ValueError("Preview key column is absent from the table.")
    key_index = columns.index(key_column)

    def index_rows(rows):
        indexed = {}
        occurrences = Counter()
        for row in rows:
            key = str(row[key_index]) if key_index < len(row) else object()
            indexed[key, occurrences[key]] = row
            occurrences[key] += 1
        return indexed

    left = index_rows(before)
    right = index_rows(after)
    return ((left.get(key), right.get(key)) for key in dict.fromkeys((*left, *right)))


def render_preview(preview: Dict[str, Any]) -> str:
    if not preview:
        return "<p>No change preview supplied.</p>"
    cols = preview.get("columns", [])
    changed = set(preview.get("changedColumns", []))
    head = "<th>Status</th>" + "".join(f"<th>{_e(c)}</th>" for c in cols)
    body = []
    for before, after in pair_preview_rows(preview):
        if before is None:
            status = "Added"
        elif after is None:
            status = "Removed"
        else:
            status = "Unchanged"

        cells = []
        for i, column in enumerate(cols):
            old = before[i] if before is not None and i < len(before) else ""
            new = after[i] if after is not None and i < len(after) else ""
            if before is not None and after is not None and column in changed and old != new:
                status = "Changed"
                cells.append(f"<td class='chg'><del>{_e(old)}</del> &rarr; <ins>{_e(new)}</ins></td>")
            else:
                cells.append(f"<td>{_e(old if after is None else new)}</td>")
        body.append(f"<tr><td>{status}</td>" + "".join(cells) + "</tr>")
    warnings = preview.get("warnings", [])
    notes = "".join(f"<li>{_e(n)}</li>" for n in preview.get("notes", []))
    warns = "".join(f"<li class='warn'>{_e(w.get('text'))}</li>" for w in warnings)
    blocked = preview.get("blocked") or any(w.get("blocking") for w in warnings)
    gate = ("Preview blocked. Resolve the blocking items before continuing." if blocked
            else "Preview only. Approve or discard the change in Revit (Keep/Discard).")
    return (
        f"<h3>{_e(preview.get('title', 'Change preview'))}</h3>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
        f"<ul>{notes}</ul><ul>{warns}</ul>"
        f"<p class='gate'>{gate}</p>"
    )


def render_board(board):
    error = "Use the JSON output of board_cli.py parse for the issue board."
    if not isinstance(board, dict) or not isinstance(board.get("header"), dict):
        raise ValueError(error)
    title = board["header"].get("title")
    issues, warnings = board.get("issues"), board.get("warnings")
    if (not isinstance(title, str) or not isinstance(issues, list)
            or not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings)):
        raise ValueError(error)

    def fields(issue):
        for name in ("assignees", "severity", "findings", "todos"):
            values = issue.get(name)
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(error)
        for name in ("element_ids", "related"):
            values = issue.get(name)
            if not isinstance(values, list) or any(type(value) is not int for value in values):
                raise ValueError(error)
        comments = issue.get("comments")
        if not isinstance(comments, list) or any(
            not isinstance(item, dict)
            or any(not isinstance(item.get(key), str) for key in ("date", "user", "text"))
            for item in comments
        ):
            raise ValueError(error)
        return (
            ("Assignees", [" ".join(issue["assignees"])] if issue["assignees"] else []),
            ("Severity", issue["severity"]),
            ("Findings", issue["findings"]),
            ("To do", issue["todos"]),
            ("Comments", [f"{c['date']} {c['user']} {c['text']}" for c in comments]),
            ("Element IDs", [", ".join(map(str, dict.fromkeys(issue["element_ids"])))] if issue["element_ids"] else []),
            ("Related issues", [", ".join(f"#{n}" for n in issue["related"])] if issue["related"] else []),
        )

    rendered = []
    for issue in issues:
        if (not isinstance(issue, dict) or type(issue.get("number")) is not int
                or issue["number"] < 1 or issue.get("status") not in ("OPEN", "IN PROGRESS", "DONE")
                or not isinstance(issue.get("title"), str)):
            raise ValueError(error)
        summary = f"#{issue['number']} [{issue['status']}] {issue['title']}"
        details = "".join(f"<dt>{label}</dt>" + "".join(f"<dd>{_e(value)}</dd>" for value in values)
                          for label, values in fields(issue) if values)
        rendered.append(f"<details><summary>{_e(summary)}</summary><dl>{details}</dl></details>")
    notices = "".join(f"<li>{_e(warning)}</li>" for warning in warnings)
    warnings_html = f"<h3>Board warnings</h3><ul class='warn'>{notices}</ul>" if notices else ""
    rows = "".join(rendered) if rendered else "<p>No issues in the board.</p>"
    return f"<h2>Issue board</h2><p>{_e(title)}</p>{warnings_html}{rows}"


STYLE = (
    "body{font-family:system-ui,sans-serif;margin:2rem;background:#f6f4ef;color:#0a0a0a}"
    "table{border-collapse:collapse;width:100%;margin:1rem 0}"
    "th,td{border:1px solid #bbb;padding:.35rem .6rem;text-align:left;font-size:.9rem}"
    ".cards{display:flex;gap:1rem}.card{padding:.8rem 1.2rem;border:1px solid #0a0a0a;border-radius:12px}"
    ".sev-error td:first-child{color:#b00020;font-weight:600}"
    ".sev-warning td:first-child{color:#8a5a00;font-weight:600}"
    "details{margin:.5rem 0}summary{cursor:pointer}dt{font-weight:600;margin-top:.5rem}"
    ".chg{background:#fff3c4}.gate{font-weight:600}.warn{color:#b00020}"
    "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}th,td{border-color:#555}}"
)


def build_page(title: str, findings: List[Dict[str, Any]], preview: Dict[str, Any], *, board=None) -> str:
    s = summarize(findings)
    cards = "".join(
        f"<div class='card'><strong>{s[k]}</strong><br>{label}</div>"
        for k, label in (("error", "errors"), ("warning", "warnings"), ("info", "info"), ("total", "total"))
    )
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        f"<title>{_e(title)}</title><style>{STYLE}</style></head><body>"
        f"<h1>{_e(title)}</h1><p>Read-only local report. No data leaves this computer.</p>"
        f"<div class='cards'>{cards}</div>"
        f"<h2>Audit findings</h2>{render_findings(findings)}"
        f"<h2>Change preview</h2>{render_preview(preview)}"
        f"{render_board(board) if board is not None else ''}"
        "</body></html>"
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Build a local read-only project page.")
    p.add_argument("--title", default="Project report")
    p.add_argument("--audit", action="append", default=[], help="Audit result JSON (repeatable)")
    p.add_argument("--preview", help="Change preview JSON")
    p.add_argument("--board", help="Issue board JSON from board_cli.py parse")
    p.add_argument("--out", required=True, help="Output HTML path")
    args = p.parse_args()

    inputs = args.audit + [path for path in (args.preview, args.board) if path]
    for path in inputs:
        if not os.path.isfile(path):
            print(f"Error: file '{path}' not found.", file=sys.stderr)
            return 1
        if os.path.exists(args.out) and os.path.samefile(path, args.out):
            print("Error: output must be separate from the input files.", file=sys.stderr)
            return 1

    try:
        findings = collect_findings([load_json(a) for a in args.audit])
        preview = load_json(args.preview) if args.preview else {}
        board = load_json(args.board) if args.board else None
        if args.board and board is None:
            raise ValueError("Use the JSON output of board_cli.py parse for the issue board.")
        page = build_page(args.title, findings, preview, board=board)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Wrote {args.out} ({len(findings)} findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
