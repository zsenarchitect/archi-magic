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
    if board["header"]["archive"] or board["header"]["next_number"] is None:
        raise ValueError("Use an active issue board for BCF import.")
    active_target = board_cli.note_target(source, body)
    archive_target = None
    existing = {marker(row) for row in board["issues"]}
    topics, warnings = bcf.read_topics(args.input)
    if args.archive:
        archive_body, _, archive, archive_source = read_board(args.archive)
        if not archive["header"]["archive"] or archive["header"]["next_number"] is not None:
            raise ValueError("Use an archive note for BCF duplicate checking.")
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


def checked_run(path, mode, status, decision, approval, note_id=None, transaction=None):
    saved = board_cli.load(bounded(path))
    record = saved.get("response", saved)
    if (record.get("mode") != mode or record.get("status") != status or record.get("decision") != decision
            or record.get("approval") != approval or record.get("result_truncated") is not False
            or type(record.get("exit_code")) is not int or record["exit_code"] != 0
            or not isinstance(record.get("run_id"), str) or not record["run_id"].strip()
            or record.get("error") is not None or not isinstance(record.get("result"), dict)
            or any(record.get(key) for key in (
                "failures", "warnings", "dialogs", "blocked", "closed_documents", "unclosed_documents", "reckless"))):
        raise ValueError("Use a complete pyRevit %s response with the required decision." % mode)
    changes = record["changes"]
    if (changes.get("other_documents") or any(changes.get(key + "_ids") for key in ("added", "modified", "deleted"))
            or changes.get("transactions") != ([transaction] if transaction is not None else [])):
        raise ValueError("The bridge effects do not belong to the reviewed board transaction.")
    for key in ("added", "deleted"):
        if type(changes.get(key + "_count")) is not int or changes[key + "_count"] != 0 or changes.get(key) != []:
            raise ValueError("The bridge reports unexpected additions or deletions.")
    modified = changes["modified"]
    expected = [] if note_id is None else [(note_id, "TextNote")]
    if (not isinstance(modified, list) or type(changes.get("modified_count")) is not int
            or changes["modified_count"] != len(modified)
            or [(str(row["id"]), row["class"]) for row in modified] != expected):
        raise ValueError("The bridge did not report exactly the reviewed board note changes.")
    counts = changes.get("by_category")
    if (not isinstance(counts, dict) or any(type(count) is not int or count <= 0 for count in counts.values())
            or sum(counts.values()) != len(expected)):
        raise ValueError("The bridge category counts do not match the exact board effects.")
    return record


def checked_readback(path, target, expected_sha1):
    record = checked_run(path, "query", "ok", "rolled_back", None)
    source = record["result"]
    text, _ = board_cli.decode_note(source)
    if (source["host_identity"] != target["host_identity"]
            or board_cli.note_target(source, text) != dict(target["active"], sha1=expected_sha1)):
        raise ValueError("The fresh board readback does not match the reviewed project, note, and text.")
    return record


def verify_import(args):
    preview = import_preview(args)
    if board_cli.load(bounded(args.preview)) != preview:
        raise ValueError("The BCF import differs from the saved reviewed preview or its viewpoint metadata.")
    plan = preview["plan"]
    if plan is None or not plan.get("board_target"):
        raise ValueError("Verification needs a reviewed import bound to a saved Revit board.")
    target = plan["board_target"]
    note = target["active"]
    intended = ib.text_sha1(plan["final_text"])
    transaction = "Issue board: " + plan.get("action", plan["mode"])

    def verify_write(path, mode, status, decision, approval):
        record = checked_run(path, mode, status, decision, approval, note["id"], transaction)
        result = record["result"]
        if (result.get("status") != "ok" or str(result.get("note_id")) != note["id"]
                or result.get("sha1_after") != intended):
            raise ValueError("The board write receipt does not match the approved import.")
        return record

    dry = verify_write(args.dry_run, "dry_run", "ok", "rolled_back", None)
    restored = checked_readback(args.dry_readback, target, note["sha1"])
    run_ids = [dry["run_id"], restored["run_id"]]
    status = "verified_dry_run"
    if args.decision:
        raw = board_cli.load(bounded(args.decision))
        decision = raw.get("response", raw).get("decision")
        if decision not in ("committed", "discarded"):
            raise ValueError("Use an explicit Revit Keep or Discard outcome.")
        kept = decision == "committed"
        final = verify_write(args.decision, "modify", "ok" if kept else "rejected", decision, "user")
        fresh = checked_readback(args.readback, target, intended if kept else note["sha1"])
        run_ids.extend((final["run_id"], fresh["run_id"]))
        status = "verified_keep" if kept else "verified_discard"
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("Use a separate pyRevit run for each write and fresh readback.")
    result = {"status": status, "note_id": note["id"], "run_ids": run_ids}
    if status == "verified_keep":
        result["metadata_after_keep"] = preview["metadata_after_keep"]
    return result


def export_topics(args):
    _, _, board, _ = read_board(args.board)
    metadata = {}
    for path in args.metadata or ():
        data = board_cli.load(bounded(path))
        if bcf.uuid_text(data["board_uuid"]) != args.board_uuid or not isinstance(data["topics"], dict):
            raise ValueError("metadata does not match this board UUID")
        for number, topic in data["topics"].items():
            if number in metadata and metadata[number] != topic:
                raise ValueError(f"Conflicting metadata for board issue {number}.")
            metadata[number] = topic
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
    verify = modes.add_parser("verify-import")
    write = modes.add_parser("export")
    for mode in (read, verify, write):
        mode.add_argument("--board", type=Path, required=True)
        mode.add_argument("--board-uuid", type=bcf.uuid_text, required=True)
    for mode in (read, verify):
        mode.add_argument("--input", type=Path, required=True)
        mode.add_argument("--archive", type=Path)
    verify.add_argument("--dry-run", type=Path, required=True)
    verify.add_argument("--dry-readback", type=Path, required=True)
    verify.add_argument("--preview", type=Path, required=True)
    verify.add_argument("--decision", type=Path)
    verify.add_argument("--readback", type=Path)
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("--author", required=True)
    write.add_argument("--metadata", type=Path, action="append", help="Repeat for each applicable metadata file.")
    args = parser.parse_args(argv)
    try:
        if args.mode == "verify-import":
            if bool(args.decision) != bool(args.readback):
                raise ValueError("Supply both the user decision and the fresh readback.")
            result = verify_import(args)
        else:
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
