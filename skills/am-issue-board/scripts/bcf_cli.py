"""Preview BCF imports and export BCF 2.1 from issue board snapshots."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from uuid import UUID, uuid5
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

import bcf_xml as bcf
import board_cli
import issue_board as ib


MAX_JSON = 8 * 1024 * 1024
MARKER = "BCF topic UUID: "
MARKER_RE = re.compile(r"BCF topic UUID: ([0-9a-f-]{36})\Z")
STATUS_EXPORT = {"OPEN": "Open", "IN PROGRESS": "In Progress", "DONE": "Closed"}
STATUS_IMPORT = {"open": "OPEN", "active": "OPEN", "in progress": "IN PROGRESS",
                 "inprogress": "IN PROGRESS", "closed": "DONE", "done": "DONE",
                 "resolved": "DONE"}


def bounded(path):
    if path.stat().st_size > MAX_JSON:
        raise ValueError("board or metadata JSON exceeds size limit")
    return path


def literal(value):
    # Text enters the board grammar only after XML decoding. Neutralize all
    # paragraph, formatting and user escape constructs before building a plan.
    value = "".join(" " if unicodedata.category(c)[0] == "C" else c for c in value)
    value = " ".join(value.split()).replace("\\", "\uff3c")
    value = re.sub(r"(\s-\s+)@", lambda m: m.group(1) + "\uff20", value)
    value = ib.ID_RE.sub(lambda m: "id " + m.group(1), value)
    return re.sub(re.escape(MARKER), "BCF source reference: ", value, flags=re.I)


def marker(issue):
    found = []
    for finding in issue["findings"]:
        if isinstance(finding, str):
            match = MARKER_RE.fullmatch(finding)
            if match:
                found.append(bcf.uuid_text(match.group(1)))
    if len(found) > 1:
        raise ValueError("board issue has ambiguous BCF provenance")
    return found[0] if found else None


def read_board(path):
    source = board_cli.load(bounded(path))
    body, formats = board_cli.decode_note(source)
    board = ib.parse_board(body, formats)
    numbers = [row["number"] for row in board["issues"]]
    if len(numbers) != len(set(numbers)):
        raise ValueError("board has duplicate issue numbers")
    return body, formats, board, source


def import_preview(args):
    body, formats, board, source = read_board(args.board)
    active_target = board_cli.note_target(source, body)
    archive_target = None
    existing = {marker(row) for row in board["issues"]}
    topics, warnings = bcf.read_topics(args.input)
    if args.archive:
        archive_body, _, archive, archive_source = read_board(args.archive)
        archive_target = board_cli.note_target(archive_source, archive_body)
        if active_target and archive_target and (
            source["host_identity"] != archive_source["host_identity"]
            or active_target["id"] == archive_target["id"]
        ):
            raise ValueError("Read distinct board and archive notes from the same saved project.")
        existing.update(marker(row) for row in archive["issues"])
    else:
        warnings.append({"kind": "archive_note_not_checked_for_duplicates"})
    ops, added, skipped = [], [], []
    for topic in topics:
        guid = topic["guid"]
        if guid in existing:
            skipped.append(guid)
            continue
        normalized = " ".join(topic["status"].lower().replace("_", " ").split())
        status = STATUS_IMPORT.get(normalized)
        if status is None:
            warnings.append({"topic": guid, "kind": "unknown_status_proposed_open"})
            status = "OPEN"
        findings = [MARKER + guid]
        if topic["description"]:
            findings.append("BCF description: " + literal(topic["description"]))
        title = literal(topic["title"])
        if not title:
            raise ValueError("topic title contains no displayable text")
        issue = {"title": title, "status": status, "findings": findings}
        ops.append({"op": "add", "issue": issue})
        added.append(topic)

    plan = ib.plan_batch(body, ops, formats) if ops else None
    metadata = {}
    if plan:
        plan["board_target"] = None
        if active_target and (not args.archive or archive_target):
            plan["board_target"] = {
                "host_identity": source["host_identity"], "active": active_target, "archive": archive_target,
            }
        else:
            warnings.append({"kind": "board_identity_required_to_apply"})
        final = ib.parse_board(plan["final_text"])
        after = {row["number"]: row for row in final["issues"]}
        if len(final["issues"]) != len(board["issues"]) + len(added) or len(after) != len(final["issues"]):
            raise ValueError("board preview changed issue count unexpectedly")
        for operation, topic, request in zip(plan["operations"], added, ops):
            number = int(operation.removeprefix("add #"))
            row = after[number]
            issue = request["issue"]
            if (row["title"] != issue["title"] or row["status"] != issue["status"]
                    or marker(row) != topic["guid"] or row["assignees"] != ["@TBD"]
                    or row["element_ids"] or row["related"] or row["todos"] or row["comments"]):
                raise ValueError("board preview does not preserve literal BCF issue data")
            metadata[str(number)] = {
                "guid": topic["guid"], "viewpoints": topic["viewpoints"],
                "source_title": topic["title"], "source_description": topic["description"],
                "source_status": topic["status"],
            }
    return {"status": "preview" if plan else "no_changes", "plan": plan,
            "metadata_after_keep": {"board_uuid": args.board_uuid, "topics": metadata},
            "skipped_topic_guids": skipped, "warnings": warnings}


def export_topics(args):
    _, _, board, _ = read_board(args.board)
    metadata = {}
    if args.metadata:
        data = board_cli.load(bounded(args.metadata))
        if bcf.uuid_text(data["board_uuid"]) != args.board_uuid or not isinstance(data["topics"], dict):
            raise ValueError("metadata does not match this board UUID")
        metadata = data["topics"]
    known = {str(row["number"]) for row in board["issues"]}
    if set(metadata) - known:
        raise ValueError("metadata contains a board issue number absent from this snapshot")
    topics, warnings = [], []
    for row in board["issues"]:
        number = row["number"]
        imported_guid = marker(row)
        guid = imported_guid or str(uuid5(UUID(args.board_uuid), str(number)))
        extra = metadata.get(str(number), {})
        if not isinstance(extra, dict):
            raise ValueError("issue metadata must be an object")
        if "guid" in extra and bcf.uuid_text(extra["guid"]) != guid:
            raise ValueError("topic metadata does not match its board issue UUID")
        viewpoints = extra.get("viewpoints", [])
        if imported_guid and viewpoints and "guid" not in extra:
            raise ValueError("imported viewpoint metadata requires its matching topic UUID")
        if not viewpoints:
            warnings.append({"issue": number, "kind": "no_viewpoint_supplied"})
        if row["element_ids"] and not any(view.get("ifc_guids") for view in viewpoints):
            warnings.append({"issue": number, "kind": "revit_element_ids_not_exported_as_ifc_guids"})
        lines = [line["text"] for line in row["lines"] if line["text"] != "Finding: " + MARKER + guid]
        topics.append({"guid": guid, "title": row["title"], "status": STATUS_EXPORT[row["status"]],
                       "description": "\n".join(lines), "viewpoints": viewpoints})

    data = bcf.write_topics(topics, args.author)
    # Stage in the destination directory and publish using an exclusive hard
    # link. A collision leaves the previous destination unchanged.
    with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as staged:
        temp = Path(staged.name)
    try:
        temp.write_bytes(data)
        os.link(temp, args.output)
    finally:
        temp.unlink()
    return {"status": "exported", "topics": len(topics), "warnings": warnings}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    read = modes.add_parser("import")
    write = modes.add_parser("export")
    for mode in (read, write):
        mode.add_argument("--board", type=Path, required=True)
        mode.add_argument("--board-uuid", type=bcf.uuid_text, required=True)
    read.add_argument("--input", type=Path, required=True)
    read.add_argument("--archive", type=Path)
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("--author", required=True)
    write.add_argument("--metadata", type=Path)
    args = parser.parse_args(argv)
    try:
        result = import_preview(args) if args.mode == "import" else export_topics(args)
        encoded = json.dumps(result, ensure_ascii=True, indent=2)
        if len(encoded) > MAX_JSON:
            raise ValueError("preview exceeds output size limit")
    except (OSError, ValueError, KeyError, AttributeError, OverflowError, TypeError, BadZipFile,
            RuntimeError, ParseError, ib.BoardError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
