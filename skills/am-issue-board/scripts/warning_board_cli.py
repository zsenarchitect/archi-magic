"""Prepare a local issue-board preview for selected Revit warning types."""
import argparse
import json
from pathlib import Path
import re
import sys
from uuid import UUID

import bcf_cli
import board_cli
import issue_board as ib

MARKER = "Revit warning type: "
MARKER_PATTERN = re.compile(re.escape(MARKER) + r"([0-9a-f-]{36})\Z")


def read_board(path, host):
    record = board_cli.load(bcf_cli.bounded(path))
    record = record.get("result", record)
    if record.get("host_identity") != host:
        raise ValueError("Read the warning report and both boards from the same saved project.")
    note = record["note"]
    text, formats = board_cli.decode_note(note)
    identity = {"id": element_id(note["id"]), "unique_id": note["unique_id"], "sha1": ib.text_sha1(text)}
    if not identity["unique_id"] or note["sha1"] != identity["sha1"]:
        raise ValueError("The board read does not contain a verified note identity and text hash.")
    board = ib.parse_board(text, formats)
    if len({item["number"] for item in board["issues"]}) != len(board["issues"]):
        raise ValueError("The board has duplicate issue numbers.")
    return text, formats, board, identity


def element_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        raise ValueError("Use decimal-string Revit element IDs.")
    return value


def warning_type(value):
    normalized = str(UUID(value))
    if normalized != value:
        raise ValueError("Use the canonical warning type ID from the report.")
    return normalized


def known_types(board):
    return {match.group(1) for issue in board["issues"] for finding in issue["findings"]
            if isinstance(finding, str) for match in [MARKER_PATTERN.fullmatch(finding)] if match}


def compute(record, board_path, archive_path, selections):
    report = record.get("result", record)
    if report.get("kind") != "revit-warning-triage" or report.get("status") != "ok":
        raise ValueError("Use the successful Revit warning triage result.")
    host = report.get("host_identity")
    if not host or not host.get("project_uid") or not re.fullmatch(r"[0-9a-f]{64}", host["path_sha256"]):
        raise ValueError("Read the warning inventory from a saved project.")
    selected = [warning_type(value) for value in selections]
    if not 1 <= len(selected) <= 20 or len(set(selected)) != len(selected):
        raise ValueError("Choose one to 20 distinct warning types.")
    groups = {warning_type(group["failure_id"]): group for group in report["groups"]}
    if len(groups) != len(report["groups"]) or sum(group["count"] for group in groups.values()) != report["warning_count"]:
        raise ValueError("The warning inventory is inconsistent.")
    text, formats, board, active = read_board(board_path, host)
    existing = known_types(board)
    archive = None
    warnings = []
    if archive_path:
        _, _, archived, archive = read_board(archive_path, host)
        if archive["id"] == active["id"]:
            raise ValueError("The active and archive boards must be different notes.")
        existing.update(known_types(archived))
    else:
        warnings.append("Archived issues were not checked for duplicates.")

    ops, added, skipped = [], [], []
    for kind in selected:
        group = groups[kind]
        if kind in existing:
            skipped.append(kind)
            continue
        instances = group["instances"]
        if not instances or len(instances) != group["count"]:
            raise ValueError("The selected warning group is incomplete.")
        ids = sorted({element_id(item) for row in instances for item in row["failing_ids"]}, key=int)
        linked = ids[:100]
        descriptions = group["descriptions"]
        label = bcf_cli.literal(descriptions[0])[:180] if descriptions else ""
        findings = [MARKER + kind, "Warning instances: %d" % group["count"]]
        if linked:
            findings.append("Failing elements: " + ", ".join("id" + item for item in linked))
        if len(ids) > len(linked):
            warnings.append("Warning type %s has %d more element IDs in the local report." % (kind, len(ids) - len(linked)))
        issue = {"title": "Revit warning: " + (label or kind), "status": "OPEN", "findings": findings}
        ops.append({"op": "add", "issue": issue})
        added.append({"failure_id": kind, "count": group["count"], "linked_ids": linked})
    plan = ib.plan_batch(text, ops, formats) if ops else None
    if plan:
        final = ib.parse_board(plan["final_text"])
        if len(final["issues"]) != len(board["issues"]) + len(added):
            raise ValueError("The preview did not add exactly one issue per selected warning type.")
        rows = {item["number"]: item for item in final["issues"]}
        for operation, request, proposed in zip(plan["operations"], ops, added):
            row = rows[int(operation.removeprefix("add #"))]
            if (row["title"] != request["issue"]["title"] or row["status"] != "OPEN"
                    or row["assignees"] != ["@TBD"] or row["findings"] != request["issue"]["findings"]
                    or row["element_ids"] != [int(item) for item in proposed["linked_ids"]]):
                raise ValueError("Warning text changed the planned issue structure.")
        # The existing apply script validates saved project, active note and archive identity before mutation.
        plan["structural_target"] = {"host_identity": host, "active": active, "archive": archive}
    return {"status": "preview" if plan else "no_changes", "plan": plan,
            "note_id": active["id"], "added": added, "skipped_failure_ids": skipped, "warnings": warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--type", dest="types", required=True, action="append")
    args = parser.parse_args()
    try:
        report = board_cli.load(bcf_cli.bounded(args.report))
        result = compute(report, args.board, args.archive, args.types)
    except (ib.BoardError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print("Warning board preview stopped: %s" % error, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
