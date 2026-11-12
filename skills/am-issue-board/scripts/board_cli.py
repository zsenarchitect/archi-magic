# -*- coding: utf-8 -*-
"""Plan board changes outside Revit (CPython 3). Prints JSON to stdout.

The READ file is the "note" object from read_board.py, saved as JSON
({"text": ..., "formats": [...], ...}). Its \\uXXXX escapes are decoded here.

    python board_cli.py parse READ.json
    python board_cli.py create  SPEC.json     SPEC = {"title_line": ..., "issues": [...], "next_number": n}
    python board_cli.py add     READ.json ISSUE.json
    python board_cli.py status  READ.json NUMBER STATUS DATE USER "comment"
    python board_cli.py comment READ.json NUMBER DATE USER "comment"
    python board_cli.py relate  READ.json A B
    python board_cli.py batch   READ.json OPS.json
    python board_cli.py mep-ops FINDINGS.json FINDING_ID [FINDING_ID ...]
    python board_cli.py mep-native-preview REPORT.json READ.json FINDING_ID [FINDING_ID ...]
    python board_cli.py archive READ.json ARCHIVE.json [--status DONE] [--title "..."]
    python board_cli.py replace READ.json NUMBER finding|todo LINES.json

OPS.json = {"ops": [...]} or a bare list. Each op is one of:
    {"op": "status", "number": N, "status": "DONE", "date": ..., "user": ..., "comment": ...}
    {"op": "comment", "number": N, "date": ..., "user": ..., "comment": ...}
    {"op": "relate", "a": A, "b": B}
    {"op": "add", "issue": {...}}
The batch makes one plan with one SHA guard, so Revit shows one Keep prompt.
LINES.json = {"lines": [...]} or a bare list. Each line is a string, or
[line, detail, detail] for a field line with detail rows.

ISSUE.json = {"title": ..., "status": "OPEN", "assignees": ["@user"], "severity": "High",
              "findings": ["... id1001 ...", ["header line", "detail row"]],
              "todos": [...], "comments": [{"date": ..., "user": ..., "text": ...}],
              "related": [2], "parent": 2}

Each plan has "final_text": show the user the changed paragraphs before the dry run.
Exit code 2 = the plan cannot be made (the message says why).
"""
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import issue_board as ib  # noqa: E402


def load(path):
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.loads(f.read())


def read_note(path):
    return decode_note(load(path))


def decode_note(note):
    if "note" in note and isinstance(note["note"], dict):
        note = note["note"]
    return ib.unescape(note["text"]), note.get("formats")


def note_target(source, body):
    if "host_identity" not in source:
        return None
    host = source["host_identity"]
    note = source["note"]
    if (
        set(host) != {"project_uid", "path_sha256"}
        or not host["project_uid"].strip()
        or not re.fullmatch(r"[0-9a-f]{64}", host["path_sha256"])
        or not re.fullmatch(r"[1-9][0-9]{0,18}", note["id"])
        or int(note["id"]) > 2**63 - 1
        or not note["unique_id"].strip()
        or note["sha1"] != ib.text_sha1(body)
    ):
        raise ValueError("Read the board again with include_identity enabled.")
    return {key: note[key] for key in ("id", "unique_id", "sha1")}


def read_archive(path):
    """(text, formats) of an archive note file, or (None, None) when the file
    is missing or holds no note: that means the archive note is created."""
    if not os.path.exists(path):
        return None, None
    note = load(path)
    if not isinstance(note, dict):
        return None, None
    if "note" in note and isinstance(note["note"], dict):
        note = note["note"]
    if not isinstance(note, dict) or "text" not in note:
        return None, None
    return ib.unescape(note["text"]), note.get("formats")


def load_ops(path):
    data = load(path)
    if isinstance(data, dict):
        data = data.get("ops")
    return data


def selected_mep_findings(report, selection):
    if not 1 <= len(selection) <= 100 or len(set(selection)) != len(selection):
        raise ValueError("Select one to 100 distinct IDs from the reviewed MEP report.")
    rows = report.get("findings") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Expected an MEP findings report.")
    reviewed = set(selection)
    findings = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise ValueError("Every report row needs a finding ID.")
        if row["id"] in reviewed:
            if row["id"] in findings:
                raise ValueError("A selected finding ID is ambiguous.")
            findings[row["id"]] = row
    if reviewed != findings.keys():
        raise ValueError("A selected finding ID is absent from the MEP report.")
    return [findings[finding_id] for finding_id in selection]


def mep_board_ops(findings, link_id=None, *, native=False):
    ops = []
    for row in findings:
        rule = row["ruleId"]
        severity = row["severity"]
        raw_element_id = row["elementId"]
        element_id = str(raw_element_id)
        if not (isinstance(rule, str) and re.fullmatch(r"MEP-[A-Z]+-[0-9]{2}", rule)
                and severity in ("error", "warning", "info")
                and type(raw_element_id) in (int, str)
                and re.fullmatch(r"[1-9][0-9]{0,18}", element_id)
                and int(element_id) <= 2**63 - 1):
            raise ValueError("A selected finding lacks a valid MEP rule, severity, or Revit element ID.")
        raw_message = row["message"]
        message = " ".join(raw_message.split()) if isinstance(raw_message, str) else ""
        if not message or not message.isprintable():
            raise ValueError("A selected finding needs a readable message.")
        # The report text may describe other IDs. The board must link only the reviewed target.
        message = ib.ID_RE.sub(r"ID \1", message)
        if native:
            message = message.replace("\\", "\uff3c")
        target = "Audited element id" + element_id if link_id is None else (
            "Linked element " + element_id + " in Revit link instance " + link_id)
        lines = [target, message]
        point = row.get("location")
        if isinstance(point, dict):
            coords = [point.get(axis) for axis in "xyz"]
            if all(type(value) in (int, float) and -float("inf") < value < float("inf") for value in coords):
                lines.append("Audit coordinates (x, y, z): " + ", ".join(map(str, coords)))
        ops.append({"op": "add", "issue": {"title": rule + " audit finding", "status": "OPEN",
                                            "severity": severity.capitalize(), "findings": lines}})
    return ops


def mep_operations(report, selection):
    if isinstance(report, dict) and str(report.get("kind", "")).startswith("mep-native-"):
        raise ValueError("Native MEP reports require mep-native-preview.")
    return {"ops": mep_board_ops(selected_mep_findings(report, selection))}


def native_mep_preview(data, board, selection):
    from mep_qa import validate_native_source

    if (set(data) != {"kind", "version", "report"} or data["kind"] != "mep-native-report"
            or type(data["version"]) is not int or data["version"] != 1):
        raise ValueError("Expected a version 1 native MEP findings report.")
    report = data["report"]
    if report["status"] not in ("audited", "incomplete_evaluation"):
        raise ValueError("Select findings from an evaluated native MEP report.")
    source = report["source"]
    validate_native_source(source)
    body, formats = decode_note(board)
    note = note_target(board, body)
    if note is None or board["host_identity"] != source["host_identity"]:
        raise ValueError("Read the board with include_identity from the audited host project.")
    findings = selected_mep_findings(report, selection)
    link_id = source["link_instance_id"]
    ops = mep_board_ops(findings, link_id, native=True)
    targets = {}
    for row in findings:
        element_id = row["elementId"]
        evidence = report["targets"][element_id]
        if (evidence["id"] != element_id or not isinstance(evidence["uid"], str)
                or not 0 < len(evidence["uid"]) <= 160):
            raise ValueError("A selected finding lacks the audited element identity.")
        targets[element_id] = evidence
    before = ib.parse_board(body)
    edit = ib.plan_batch(body, ops, formats)
    edit.pop("kind")
    after = ib.parse_board(edit["final_text"])
    new_rows = after["issues"][len(before["issues"]):]
    if (after["issues"][:len(before["issues"])] != before["issues"] or len(new_rows) != len(findings)
            or len({row["number"] for row in after["issues"]}) != len(after["issues"])
            or any(row["element_ids"] != ([] if link_id else [int(finding["elementId"])])
                   for row, finding in zip(new_rows, findings))):
        raise ValueError("The MEP board preview changed an existing issue or added an unexpected element link.")
    return {
        "kind": "mep-native-board-plan", "version": 1, "edit": edit,
        "board_target": {"host_identity": source["host_identity"], "active": note, "archive": None},
        "source_guard": {"source": source, "targets": targets, "clearance": report["clearance"],
                         "revit_year": report["revit_year"]},
    }


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 2
    cmd = argv[0]
    try:
        if cmd == "create":
            spec = load(argv[1])
            out = ib.plan_create(spec["title_line"], spec.get("issues", []), spec.get("next_number"))
        elif cmd == "mep-ops":
            out = mep_operations(load(argv[1]), argv[2:])
        elif cmd == "mep-native-preview":
            out = native_mep_preview(load(argv[1]), load(argv[2]), argv[3:])
        elif cmd == "batch":
            text, formats = read_note(argv[1])
            out = ib.plan_batch(text, load_ops(argv[2]), formats)
        elif cmd == "archive":
            status = "DONE"
            title = None
            rest = argv[3:]
            i = 0
            while i < len(rest):
                if rest[i] == "--status" and i + 1 < len(rest):
                    status = rest[i + 1]
                    i += 2
                elif rest[i] == "--title" and i + 1 < len(rest):
                    title = rest[i + 1]
                    i += 2
                else:
                    sys.stderr.write("unknown archive option: %s\n" % rest[i])
                    return 2
            text, formats = read_note(argv[1])
            atext, aformats = read_archive(argv[2])
            out = ib.plan_archive(text, status, formats, atext, aformats, title)
        elif cmd == "replace":
            text, formats = read_note(argv[1])
            out = ib.plan_replace_field(text, int(argv[2]), argv[3], load(argv[4]), formats)
        else:
            text, formats = read_note(argv[1])
            if cmd == "parse":
                board = ib.parse_board(text, formats)
                counts = ib.summary(board)
                for i in board["issues"]:
                    del i["lines"]
                out = {"summary": counts, "header": board["header"],
                       "issues": board["issues"], "warnings": board["warnings"]}
            elif cmd == "add":
                out = ib.plan_add_issue(text, load(argv[2]), formats)
            elif cmd == "status":
                out = ib.plan_set_status(text, int(argv[2]), argv[3], argv[4], argv[5], argv[6], formats)
            elif cmd == "comment":
                out = ib.plan_comment(text, int(argv[2]), argv[3], argv[4], argv[5], formats)
            elif cmd == "relate":
                out = ib.plan_relate(text, int(argv[2]), int(argv[3]), formats)
            else:
                sys.stderr.write("unknown command: %s\n" % cmd)
                return 2
    except (ib.BoardError, OSError, IndexError, KeyError, TypeError, AttributeError, ValueError) as e:
        sys.stderr.write("cannot plan: %s\n" % e)
        return 2
    sys.stdout.write(json.dumps(out, indent=1, ensure_ascii=True))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
