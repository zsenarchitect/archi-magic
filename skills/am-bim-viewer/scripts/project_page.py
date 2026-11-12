"""Local project page generator (issue #187 Phase 1).

Builds one self-contained, read-only HTML page from local JSON files:
- audit findings (model_health.py, mep_qa.py, dwg_cad_audit.py, check_packs.py output),
- optional change-preview JSON (am-safe-change),
- optional read-only issue board JSON (board_cli.py parse),
- optional BIM JSON export for an offline model viewer.

No network call. No data leaves the machine. The page cannot change a model.
Model changes stay behind the Revit Keep/Discard step.

Usage:
    python project_page.py --title "Project A" --audit a.json [--audit b.json] \
        [--preview p.json] [--board board.json] [--model model.json] --out project.html
"""

from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import datetime
import hashlib
import html
import math
from itertools import zip_longest
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Dict, List

SEVERITIES = ("error", "warning", "info")


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_response(value, modes, error):
    if isinstance(value, dict) and "response" in value:
        value = value["response"]
    if (not isinstance(value, dict) or value.get("status") != "ok"
            or value.get("mode") not in (*modes, None)
            or value.get("decision") not in ("rolled_back", "no_changes", None)
            or value.get("error") or value.get("approval")
            or any(value.get(field) for field in ("failures", "dialogs", "blocked", "warnings", "unclosed_documents"))
            or value.get("exit_code", 0) != 0):
        raise ValueError(error)
    changes = value.get("changes")
    if changes is not None:
        if not isinstance(changes, dict) or changes.get("other_documents"):
            raise ValueError(error)
        if value.get("mode") != "dry_run" and any(changes.get(field) for field in (
                "added", "modified", "deleted", "added_ids", "modified_ids", "deleted_ids",
                "added_count", "modified_count", "deleted_count", "by_category")):
            raise ValueError(error)
    return value


def read_result_pages(records, modes, error):
    run = None
    total = None
    parts = {}
    for item in records:
        record = run_response(item, modes, error)
        page = record.get("result_page")
        identifier = record.get("run_id")
        if (not isinstance(page, dict) or record.get("result_truncated") is not True
                or not isinstance(identifier, str) or not identifier.strip()
                or not isinstance(page.get("text"), str)
                or any(type(page.get(field)) is not int for field in ("offset", "length", "total_length"))):
            raise ValueError(error)
        start, length, reported_total = (page[field] for field in ("offset", "length", "total_length"))
        # pyRevit offsets count UTF-16 code units, including split surrogate pairs.
        chunk = page["text"].encode("utf-16-le", errors="surrogatepass")
        identity = (identifier, record.get("mode"), record.get("decision"))
        if run is None:
            run, total = identity, reported_total
        if (identity != run or total != reported_total or start < 0 or length < 1
                or start in parts or len(chunk) != length * 2 or start + length > total):
            raise ValueError(error)
        parts[start] = chunk
    cursor = 0
    result = bytearray()
    for start, chunk in sorted(parts.items()):
        if start != cursor:
            raise ValueError(error)
        result.extend(chunk)
        cursor += len(chunk) // 2
    if not cursor or cursor != total:
        raise ValueError(error)
    try:
        return json.loads(result.decode("utf-16-le"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(error) from exc


def read_saved_result(value, modes, error):
    if isinstance(value, list):
        return read_result_pages(value, modes, error)
    if not isinstance(value, dict):
        raise ValueError(error)
    if not any(field in value for field in ("response", "result", "result_page", "result_truncated")):
        return value
    response = run_response(value, modes, error)
    if response.get("result_truncated") is True:
        return read_result_pages([response], modes, error)
    if response.get("result_truncated", False) is not False or not isinstance(response.get("result"), dict):
        raise ValueError(error)
    return response["result"]


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


def model_health_runs(audits):
    runs = []
    for audit in audits:
        if "metrics" not in audit:
            continue
        try:
            recorded = datetime.strptime(audit["timestamp"], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            recorded = datetime.min
        runs.append((recorded, audit))
    return [audit for _, audit in sorted(runs, key=lambda entry: entry[0], reverse=True)]


def _e(value: Any) -> str:
    """Escape any value for HTML. Model text is data, never markup."""
    return html.escape("" if value is None else str(value), quote=True)


def render_model_health(audits):
    if not audits:
        return ""
    rows = []
    for audit in audits:
        counts = Counter(metric["status"] for metric in audit["metrics"])
        rows.append(
            f"<tr><th scope='row'>{_e(audit['model'])}</th><td>{_e(audit['timestamp'])}</td>"
            f"<td>{len(audit['metrics'])}</td><td>{counts['bad']}</td><td>{counts['warn']}</td></tr>"
        )
    return (
        "<h2>Model health audits</h2><table>"
        "<thead><tr><th>Model</th><th>Recorded time</th><th>Checks</th>"
        "<th>Errors</th><th>Warnings</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


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


def read_change_preview(value):
    error = "Use a complete change-preview result from a supported recipe."
    value = read_saved_result(value, ("query", "dry_run"), error)
    if isinstance(value, dict) and "payload" in value:
        if not isinstance(value["payload"], str):
            raise ValueError(error)
        value = json.loads(value["payload"])
    if isinstance(value, dict) and "preview" in value:
        value = value["preview"]
    if not isinstance(value, dict) or value.get("kind", "change-preview") != "change-preview":
        raise ValueError(error)
    columns = value.get("columns")
    if (not isinstance(columns, list) or not columns or any(not isinstance(name, str) for name in columns)
            or len(set(columns)) != len(columns)):
        raise ValueError(error)
    for name in ("before", "after"):
        rows = value.get(name)
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) > len(columns) for row in rows):
            raise ValueError(error)
    changed = value.get("changedColumns", [])
    warnings = value.get("warnings", [])
    if (not isinstance(changed, list) or any(column not in columns for column in changed)
            or not isinstance(warnings, list) or any(not isinstance(warning, dict)
                or not isinstance(warning.get("text"), str) for warning in warnings)
            or not isinstance(value.get("notes", []), list)
            or any(type(value.get(flag, False)) is not bool for flag in ("review_only", "blocked"))):
        raise ValueError(error)
    if "deletions" in value:
        entries = value["deletions"]
        if not isinstance(entries, list) or len(entries) > 5000:
            raise ValueError("Use a bounded list of planned deletions.")
        deletions = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("Name the ID, category, and element for each deletion.")
            identifier = issue_element_id(entry.get("id"))
            category, name = entry.get("category"), entry.get("name")
            if (identifier is None or identifier in seen
                    or any(not isinstance(text, str) or not text.strip() or len(text) > 4096
                           for text in (category, name))):
                raise ValueError("Use distinct exact deletion IDs and provide their categories and names.")
            seen.add(identifier)
            deletions.append({"id": identifier, "category": category, "name": name})
        value = dict(value, deletions=deletions)
    return value


def preview_deletion_lines(preview):
    deletions = preview.get("deletions", [])
    if not deletions:
        return []
    count = len(deletions)
    heading = f"Delete: {count} {'element' if count == 1 else 'elements'}"
    return [heading, *(f"ID {row['id']} | {row['category']} | {row['name']}" for row in deletions)]


def render_preview(preview: Dict[str, Any], viewer_name=None, model_facets=None) -> str:
    if not preview:
        return "<p>No change preview supplied.</p>"
    cols = preview.get("columns", [])
    changed = set(preview.get("changedColumns", []))
    head = "<th>Status</th>" + "".join(f"<th>{_e(c)}</th>" for c in cols)
    id_columns = [i for i, name in enumerate(cols) if name.casefold().replace(" ", "") == "elementid"]
    id_column = id_columns[0] if len(id_columns) == 1 else None
    rows = []
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
        link = ""
        if (viewer_name and model_facets and id_column is not None
                and status in ("Changed", "Removed") and id_column < len(before)):
            identifier = issue_element_id(before[id_column])
            same_element = after is None or (id_column < len(after) and issue_element_id(after[id_column]) == identifier)
            if identifier and same_element and model_facets.get(identifier) is not None:
                link = frame_element_link(viewer_name, [identifier], "Frame element")
        rows.append((f"<tr><td>{status}</td>" + "".join(cells), link))
    if any(link for _, link in rows):
        head += "<th>Model</th>"
        body = "".join(f"{row}<td>{link}</td></tr>" for row, link in rows)
    else:
        body = "".join(f"{row}</tr>" for row, _ in rows)
    warnings = preview.get("warnings", [])
    notes = "".join(f"<li>{_e(n)}</li>" for n in preview.get("notes", []))
    warns = "".join(
        f"<li class='warn'>{_e(w['row']) + ' · ' if w.get('row') is not None else ''}{_e(w['text'])}</li>"
        for w in warnings
    )
    blocked = preview.get("blocked") or any(w.get("blocking") for w in warnings)
    if blocked:
        gate = "Preview blocked. Resolve the blocking items before continuing."
    elif preview.get("review_only") is True:
        gate = "Review only. Use the recipe’s model change workflow to continue."
    else:
        gate = "Preview only. Approve or discard the change in Revit (Keep/Discard)."
    deletion_lines = preview_deletion_lines(preview)
    deletion_list = ""
    if deletion_lines:
        title, *entries = deletion_lines
        deletion_list = f"<h4>{_e(title)}</h4><ul>" + "".join(f"<li>{_e(line)}</li>" for line in entries) + "</ul>"
    return (
        f"<h3>{_e(preview.get('title', 'Change preview'))}</h3>"
        f"{deletion_list}"
        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        f"<ul>{notes}</ul><ul>{warns}</ul>"
        f"<p class='gate'>{gate}</p>"
    )


BOARD_FILTER_SCRIPT = """(() => {
  const board = document.getElementById('issue-board');
  const filters = board.querySelector('[data-board-filters]');
  if (!filters) return;
  const selects = [...filters.querySelectorAll('select')];
  const rows = [...board.querySelectorAll('details[data-facets]')]
    .map(row => [row, JSON.parse(row.dataset.facets)]);
  const status = filters.querySelector('output');
  function filter() {
    const choices = Object.fromEntries(selects.map(select => [select.name, select.value]));
    const location = ['category', 'level'].filter(name => choices[name]);
    let shown = 0;
    for (const [row, facets] of rows) {
      const severityMatches = !choices.severity || facets.severity.includes(choices.severity);
      const locationMatches = !location.length || facets.elements.some(element =>
        location.every(name => element[name] === choices[name]));
      row.hidden = !(severityMatches && locationMatches);
      if (!row.hidden) shown += 1;
    }
    status.textContent = shown ? `${shown} of ${rows.length} issues` : 'No matching issues.';
  }
  filters.addEventListener('change', filter);
  filter();
  filters.hidden = false;
})();"""
BOARD_FILTER_HASH = base64.b64encode(hashlib.sha256(BOARD_FILTER_SCRIPT.encode("utf-8")).digest()).decode("ascii")
BOARD_FACETS = ("severity", "category", "level")


def issue_element_id(value):
    if type(value) is int:
        value = str(value)
    if (not isinstance(value, str) or not value.isascii() or not value.isdecimal()
            or len(value) > 19 or value.startswith("0") or int(value) > 2**63 - 1):
        return None
    return value


def frame_element_link(viewer_name, identifiers, label):
    href = f"{viewer_name}#elements={','.join(identifiers)}"
    return f"<a href='{_e(href)}'>{_e(label)}</a>"


def model_issue_facets(model):
    """Index category and level by exact model export ID, omitting ambiguous IDs."""
    if not isinstance(model, dict) or not isinstance(model.get("elements"), list):
        raise ValueError("Use a BIM model export with an elements list.")
    indexed = {}
    for element in model["elements"]:
        if not isinstance(element, dict):
            continue
        identifier = issue_element_id(element.get("id"))
        if identifier is None:
            continue
        if identifier in indexed:
            indexed[identifier] = None
            continue
        indexed[identifier] = {name: value.strip() for name in ("category", "level")
                               if isinstance(value := element.get(name), str) and value.strip()}
    return indexed


def render_board(board, model_facets=None, viewer_name=None):
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
    options = {name: set() for name in BOARD_FACETS}
    for issue in issues:
        if (not isinstance(issue, dict) or type(issue.get("number")) is not int
                or issue["number"] < 1 or issue.get("status") not in ("OPEN", "IN PROGRESS", "DONE")
                or not isinstance(issue.get("title"), str)):
            raise ValueError(error)
        summary = f"#{issue['number']} [{issue['status']}] {issue['title']}"
        details = "".join(f"<dt>{label}</dt>" + "".join(f"<dd>{_e(value)}</dd>" for value in values)
                          for label, values in fields(issue) if values)
        severity = sorted({value.strip() for value in issue["severity"] if value.strip()})
        options["severity"].update(severity)
        matches = []
        issue_ids = [identifier for item in dict.fromkeys(issue["element_ids"])
                     if (identifier := issue_element_id(item)) is not None]
        for identifier in issue_ids:
            attributes = model_facets.get(identifier) if model_facets else None
            if attributes is not None:
                matches.append(attributes)
                for name, value in attributes.items():
                    options[name].add(value)
        link = ""
        if viewer_name and matches:
            label = "Frame issue elements" if len(matches) == len(issue_ids) else f"Frame {len(matches)} of {len(issue_ids)} issue elements"
            link = "<p>" + frame_element_link(viewer_name, issue_ids, label) + "</p>"
        data = json.dumps({"severity": severity, "elements": matches}, ensure_ascii=True)
        rendered.append(f"<details data-facets='{_e(data)}'><summary>{_e(summary)}</summary>{link}<dl>{details}</dl></details>")
    notices = "".join(f"<li>{_e(warning)}</li>" for warning in warnings)
    warnings_html = f"<h3>Board warnings</h3><ul class='warn'>{notices}</ul>" if notices else ""
    rows = "".join(rendered) if rendered else "<p>No issues in the board.</p>"
    controls = []
    for name, values in options.items():
        if not values:
            continue
        choices = "".join(f"<option value='{_e(value)}'>{_e(value)}</option>"
                          for value in sorted(values, key=lambda value: (value.casefold(), value)))
        controls.append(f"<label>{name}<select name='{name}'><option value=''>All</option>{choices}</select></label>")
    filters = ("<div class='board-filters' data-board-filters hidden>" + "".join(controls)
               + "<output aria-live='polite'></output></div>") if controls else ""
    return f"<section id='issue-board'><h2>Issue board</h2><p>{_e(title)}</p>{warnings_html}{filters}{rows}</section>"


STYLE = (
    "body{font-family:system-ui,sans-serif;margin:2rem;background:#f6f4ef;color:#0a0a0a}"
    "table{border-collapse:collapse;width:100%;margin:1rem 0}"
    "th,td{border:1px solid #bbb;padding:.35rem .6rem;text-align:left;font-size:.9rem}"
    ".cards{display:flex;gap:1rem}.card{padding:.8rem 1.2rem;border:1px solid #0a0a0a;border-radius:12px}"
    ".sev-error td:first-child{color:#b00020;font-weight:600}"
    ".sev-warning td:first-child{color:#8a5a00;font-weight:600}"
    "details{margin:.5rem 0}summary{cursor:pointer}dt{font-weight:600;margin-top:.5rem}"
    ".board-filters:not([hidden]){display:flex;gap:.7rem 1.2rem;flex-wrap:wrap;align-items:center}"
    ".board-filters label{display:inline-flex;gap:.4rem;align-items:center}"
    ".board-filters select{font:inherit;color:inherit;background:transparent;border:1px solid currentColor;padding:.25rem}"
    ".chg{background:#fff3c4}.gate{font-weight:600}.warn{color:#b00020}"
    "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}th,td{border-color:#555}}"
)


def build_page(title: str, findings: List[Dict[str, Any]], preview: Dict[str, Any], *,
               board=None, viewer_name=None, model_health=(), model_facets=None) -> str:
    s = summarize(findings)
    cards = "".join(
        f"<div class='card'><strong>{s[k]}</strong><br>{label}</div>"
        for k, label in (("error", "errors"), ("warning", "warnings"), ("info", "info"), ("total", "total"))
    )
    model_link = f"<p><a href='{_e(viewer_name)}'>Open 3D model</a></p>" if viewer_name else ""
    board_html = render_board(board, model_facets, viewer_name) if board is not None else ""
    script_policy = f"; script-src 'sha256-{BOARD_FILTER_HASH}'" if board_html else ""
    script = f"<script>{BOARD_FILTER_SCRIPT}</script>" if board_html else ""
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'{script_policy}\">"
        f"<title>{_e(title)}</title><style>{STYLE}</style></head><body>"
        f"<h1>{_e(title)}</h1><p>Read-only local report. No data leaves this computer.</p>"
        f"{model_link}<div class='cards'>{cards}</div>"
        f"{render_model_health(model_health)}"
        f"<h2>Audit findings</h2>{render_findings(findings)}"
        f"<h2>Change preview</h2>{render_preview(preview, viewer_name, model_facets)}"
        f"{board_html}{script}"
        "</body></html>"
    )


def write_project_with_model(output, model, title, findings, preview, board, model_health):
    builder = Path(__file__).resolve().with_name("build-viewer.mjs")
    with TemporaryDirectory(prefix=".project-viewer-", dir=output.parent) as temporary:
        staging = Path(temporary)
        built = staging / "viewer.html"
        source = model.resolve()
        data = load_json(source)
        if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
            raise ValueError("Use a BIM model export with an elements list.")
        facets = model_issue_facets(data) if board is not None or preview else None
        normalized_ids = False
        # JavaScript rounds large JSON integers even when the viewer has no issue links.
        for element in data["elements"]:
            if not isinstance(element, dict):
                continue
            identifier = element.get("id")
            if type(identifier) is int and abs(identifier) > 2**53 - 1:
                element["id"] = str(identifier)
                normalized_ids = True
        if normalized_ids or facets is not None:
            source = staging / "model.json"
            source.write_text(json.dumps(data, ensure_ascii=True, allow_nan=False), encoding="utf-8")
        del data
        try:
            subprocess.run(
                ["node", str(builder), "--offline", str(source), str(built)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise ValueError("Install Node.js to build the offline model viewer.") from error
        except subprocess.CalledProcessError as error:
            raise ValueError("Could not build the model viewer. Check the BIM JSON export and run npm ci in am-bim-viewer.") from error

        viewer_bytes = built.read_bytes()
        digest = hashlib.sha256(viewer_bytes).hexdigest()
        viewer = output.with_name(f"model-{digest}.viewer.html")
        if viewer == output:
            raise ValueError("The project page and model viewer need separate output names.")
        if viewer.is_symlink() or (viewer.exists() and (not viewer.is_file() or viewer.read_bytes() != viewer_bytes)):
            raise ValueError("The model viewer output name is already in use by another file.")

        page = staging / "report.html"
        page.write_text(build_page(title, findings, preview, board=board, viewer_name=viewer.name,
                                   model_health=model_health, model_facets=facets), encoding="utf-8")
        if not viewer.exists():
            os.replace(built, viewer)
        os.replace(page, output)


def main() -> int:
    p = argparse.ArgumentParser(description="Build a local read-only project page.")
    p.add_argument("--title", default="Project report")
    p.add_argument("--audit", action="append", default=[], help="Audit result JSON (repeatable)")
    p.add_argument("--preview", help="Change preview JSON")
    p.add_argument("--board", help="Issue board JSON from board_cli.py parse")
    p.add_argument("--model", help="BIM JSON export for the offline 3D viewer")
    p.add_argument("--out", required=True, help="Output HTML path")
    args = p.parse_args()

    inputs = args.audit + [path for path in (args.preview, args.board, args.model) if path]
    for path in inputs:
        if not os.path.isfile(path):
            print(f"Error: file '{path}' not found.", file=sys.stderr)
            return 1
        if os.path.exists(args.out) and os.path.samefile(path, args.out):
            print("Error: output must be separate from the input files.", file=sys.stderr)
            return 1

    try:
        audits = [read_saved_result(load_json(a), ("query",), "Use a complete read-only audit result.")
                  for a in args.audit]
        findings = collect_findings(audits)
        model_health = model_health_runs(audits)
        preview = read_change_preview(load_json(args.preview)) if args.preview else {}
        board = load_json(args.board) if args.board else None
        if args.board and board is None:
            raise ValueError("Use the JSON output of board_cli.py parse for the issue board.")
        output = Path(args.out).absolute()
        if args.model:
            write_project_with_model(output, Path(args.model), args.title, findings, preview, board, model_health)
        else:
            output.write_text(build_page(args.title, findings, preview, board=board, model_health=model_health),
                              encoding="utf-8")
    except (ValueError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {args.out} ({len(findings)} findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
