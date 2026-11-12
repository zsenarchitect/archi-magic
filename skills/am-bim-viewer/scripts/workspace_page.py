"""Render the installed office workspace shell and describe its review rows."""

import base64
import hashlib
from pathlib import Path

from project_page import collect_findings, pair_preview_rows


ASSETS = Path(__file__).resolve().parents[1] / "workspace"
SCRIPT = (ASSETS / "workspace.js").read_text(encoding="utf-8")
SCRIPT_HASH = base64.b64encode(hashlib.sha256(SCRIPT.encode("utf-8")).digest()).decode("ascii")
SHELL = (ASSETS / "index.html").read_text(encoding="utf-8")


def page(origin):
    socket_origin = origin.replace("https://", "wss://", 1)
    policy = (f"default-src 'none'; script-src 'sha256-{SCRIPT_HASH}'; style-src 'unsafe-inline'; "
              f"connect-src {origin} {socket_origin}; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    return SHELL.replace("<!-- workspace script -->", f"<script>{SCRIPT}</script>"), policy


def describe_artifact(artifact):
    freshness = artifact["freshness"]
    if freshness == "unavailable":
        return {"freshness": freshness, "rows": []}
    kind, data = artifact["kind"], artifact["data"]
    notices = []
    if kind == "audit":
        rows = [
            {"title": " · ".join(str(value) for value in (row.get("severity"), row.get("ruleId")) if value),
             "details": [str(value) for value in (row.get("message"), row.get("target") or row.get("elementId"),
                                                 row.get("source")) if value is not None]}
            for row in collect_findings([data])
        ]
    elif kind == "preview":
        columns = data["columns"]
        if data.get("blocked"):
            notices.append("This preview is blocked.")
        if data.get("review_only"):
            notices.append("This preview is review only.")
        notices.extend(warning["text"] for warning in data.get("warnings", []))
        rows = [
            {"title": f"Change {index}",
             "details": [f"{name} · {format_row(columns, values)}"
                         for name, values in (("Before", before), ("After", after))]}
            for index, (before, after) in enumerate(pair_preview_rows(data), 1)
        ]
    else:
        notices = [data["header"]["title"], *data["warnings"]]
        rows = [
            {"title": f"#{row['number']} {row['title']}",
             "details": [f"Revit status · {row['status']}", *row["assignees"], *row["severity"],
                         *row["findings"], *row["todos"],
                         *(f"Element {value}" for value in row["element_ids"]),
                         *(f"Related #{value}" for value in row["related"]), *(f"{c['date']} {c['user']} · {c['text']}" for c in row["comments"])]}
            for row in data["issues"]
        ]
    return {"freshness": freshness, "kind": kind, "generation": artifact["generation"],
            "notices": notices, "rows": [dict(row, target=target) for row, target in zip(rows, artifact["targets"], strict=True)]}


def format_row(columns, values):
    if values is None:
        return "No row"
    return " · ".join(f"{column} = {value}" for column, value in zip(columns, values))
