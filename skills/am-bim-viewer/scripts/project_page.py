"""Local project page generator (issue #187 Phase 1).

Builds one self-contained, read-only HTML page from local JSON files:
- audit findings (mep_qa.py, dwg_cad_audit.py, check_packs.py output),
- optional change-preview JSON (am-safe-change).

No network call. No data leaves the machine. The page cannot change a model.
Model changes stay behind the Revit Keep/Discard step.

Usage:
    python project_page.py --title "Project A" --audit a.json [--audit b.json] \
        [--preview p.json] --out project.html
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from typing import Any, Dict, List

SEVERITIES = ("error", "warning", "info")


def load_json(path: str) -> Any:
    with open(path, "rb") as f:
        return json.load(f)


def collect_findings(audits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge findings from several audit result files."""
    merged: List[Dict[str, Any]] = []
    for audit in audits:
        source = audit.get("drawing_name") or audit.get("source") or "audit"
        for f in audit.get("findings", []):
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


def render_preview(preview: Dict[str, Any]) -> str:
    if not preview:
        return "<p>No change preview supplied.</p>"
    cols = preview.get("columns", [])
    changed = set(preview.get("changedColumns", []))
    before = preview.get("before", [])
    after = preview.get("after", [])
    head = "".join(f"<th>{_e(c)}</th>" for c in cols)
    body = []
    for b_row, a_row in zip(before, after):
        cells = []
        for i, _ in enumerate(cols):
            b = b_row[i] if i < len(b_row) else ""
            a = a_row[i] if i < len(a_row) else ""
            if cols[i] in changed and b != a:
                cells.append(f"<td class='chg'><del>{_e(b)}</del> &rarr; <ins>{_e(a)}</ins></td>")
            else:
                cells.append(f"<td>{_e(a)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    notes = "".join(f"<li>{_e(n)}</li>" for n in preview.get("notes", []))
    warns = "".join(f"<li class='warn'>{_e(w.get('text'))}</li>" for w in preview.get("warnings", []))
    return (
        f"<h3>{_e(preview.get('title', 'Change preview'))}</h3>"
        f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
        f"<ul>{notes}</ul><ul>{warns}</ul>"
        "<p class='gate'>Preview only. Approve or discard the change in Revit (Keep/Discard).</p>"
    )


STYLE = (
    "body{font-family:system-ui,sans-serif;margin:2rem;background:#f6f4ef;color:#0a0a0a}"
    "table{border-collapse:collapse;width:100%;margin:1rem 0}"
    "th,td{border:1px solid #bbb;padding:.35rem .6rem;text-align:left;font-size:.9rem}"
    ".cards{display:flex;gap:1rem}.card{padding:.8rem 1.2rem;border:1px solid #0a0a0a;border-radius:12px}"
    ".sev-error td:first-child{color:#b00020;font-weight:600}"
    ".sev-warning td:first-child{color:#8a5a00;font-weight:600}"
    ".chg{background:#fff3c4}.gate{font-weight:600}.warn{color:#b00020}"
    "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}th,td{border-color:#555}}"
)


def build_page(title: str, findings: List[Dict[str, Any]], preview: Dict[str, Any]) -> str:
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
        "</body></html>"
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Build a local read-only project page.")
    p.add_argument("--title", default="Project report")
    p.add_argument("--audit", action="append", default=[], help="Audit result JSON (repeatable)")
    p.add_argument("--preview", help="Change preview JSON")
    p.add_argument("--out", required=True, help="Output HTML path")
    args = p.parse_args()

    for path in args.audit + ([args.preview] if args.preview else []):
        if not os.path.isfile(path):
            print(f"Error: file '{path}' not found.", file=sys.stderr)
            return 1

    findings = collect_findings([load_json(a) for a in args.audit])
    preview = load_json(args.preview) if args.preview else {}
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(build_page(args.title, findings, preview))
    print(f"Wrote {args.out} ({len(findings)} findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
