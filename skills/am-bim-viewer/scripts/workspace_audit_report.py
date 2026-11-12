"""Build a printable office audit report from accepted, sanitized publications."""

from datetime import datetime, timezone
import re

from project_page import _e, collect_findings, render_findings, render_model_health, summarize
from workspace_review_state import canonical


MAX_REPORT_INPUT = 8 * 1024 * 1024
MAX_REPORT_HTML = 32 * 1024 * 1024
RULE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
RULE_DESCRIPTIONS = {
    "CAD-LAY-01": "CAD layer naming check",
    "CAD-XREF-01": "CAD external reference check",
}


class AuditReportTooLarge(ValueError):
    pass


def producer_timestamp(value):
    if isinstance(value, str) and len(value) <= 64:
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            pass
    return "Unavailable"


def build_audit_report(project, revision, selected):
    remaining = MAX_REPORT_INPUT
    provenance, findings, health = [], [], []
    undescribed = 0
    for name, artifact in selected:
        data = artifact.get("data")
        timestamp = "Unavailable"
        if data is not None:
            remaining -= len(canonical(data))
            if remaining < 0:
                raise AuditReportTooLarge("Select fewer audits for this report.")
            timestamp = producer_timestamp(data.get("timestamp"))
            for finding in collect_findings([data]):
                rule = finding.get("ruleId")
                rule = rule if isinstance(rule, str) and RULE_ID.fullmatch(rule) else "Unspecified"
                description = RULE_DESCRIPTIONS.get(rule)
                undescribed += description is None
                findings.append({"severity": finding["severity"], "ruleId": rule,
                                 "message": description or "", "source": name})
            if "metrics" in data:
                health.append({"model": project, "timestamp": timestamp,
                               "metrics": [{"status": metric["status"]} for metric in data["metrics"]]})
        values = (name, artifact.get("generation", "Unavailable"), artifact["freshness"], timestamp)
        provenance.append("<tr>" + "".join(f"<td>{_e(value)}</td>" for value in values) + "</tr>")
    totals = summarize(findings)
    exported = datetime.now(timezone.utc).isoformat(timespec="seconds")
    html = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Audit report</title>'
        "<style>body{font:16px/1.5 system-ui;margin:2rem}table{border-collapse:collapse;width:100%;margin-bottom:1rem}"
        "td,th{text-align:left;padding:.3rem .5rem;border-bottom:1px solid #aaa}"
        "td:nth-child(2){white-space:nowrap}thead{display:table-header-group}tr{break-inside:avoid}"
        "</style></head><body>"
        f"<h1>Audit report · {_e(project)}</h1><p>Review revision {_e(revision)} · Exported {_e(exported)}</p>"
        "<h2>Publications</h2><table><thead><tr><th>Slot</th><th>Generation</th><th>Freshness</th>"
        "<th>Recorded time</th></tr></thead><tbody>" + "".join(provenance) + "</tbody></table>"
        + render_model_health(health)
        + f"<h2>Audit findings</h2><p>{totals['error']} errors · {totals['warning']} warnings · {totals['info']} info</p>"
        + (f"<p>{undescribed} findings have no installed description.</p>" if undescribed else "")
        + render_findings(findings) + "</body></html>"
    ).encode("utf-8")
    if len(html) > MAX_REPORT_HTML:
        raise AuditReportTooLarge("Select fewer audits for this report.")
    return html
