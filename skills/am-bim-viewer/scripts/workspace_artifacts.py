"""Publish completed project artifacts into configured office workspace slots."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from uuid import uuid4

from project_page import collect_findings, pair_preview_rows, read_change_preview, read_saved_result, render_board
from workspace_documents import MAX_DOCUMENT_INPUT, MAX_DOCUMENT_PUBLICATION, normalize_document, validate_document
from workspace_model import MAX_MODEL_BYTES, validate_model
from workspace_review_state import canonical, unique_object


MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
ARTIFACT_KINDS = ("audit", "preview", "board", "model", "document")


def slot_limit(slot):
    return {"model": MAX_MODEL_BYTES, "document": MAX_DOCUMENT_PUBLICATION}.get(slot["kind"], MAX_ARTIFACT_BYTES)


@contextmanager
def open_json_file(name, limit, directory=None):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("Use a regular JSON file without links.")
        if metadata.st_size > limit:
            raise ValueError("The workspace JSON exceeds its byte limit.")
        yield stream, metadata


def read_json_stream(stream, limit):
    raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("The workspace JSON exceeds its byte limit.")
    return json.loads(raw, object_pairs_hook=unique_object)


def read_json_file(name, limit, directory=None):
    with open_json_file(name, limit, directory) as (stream, _):
        return read_json_stream(stream, limit)


def slot_path(slot):
    path = slot["path"]
    if (not isinstance(path, str) or not path.endswith(".json")
            or any(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part) is None for part in path.split("/"))):
        raise ValueError("Configure a relative, non-hidden JSON artifact path.")
    return PurePosixPath(path)


@contextmanager
def slot_directory(root, slot):
    path = slot_path(slot)
    root = Path(root)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("Configure an absolute physical project directory.")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root.anchor, flags)
    try:
        for part in (*root.parts[1:], *path.parts[:-1]):
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory, path.name
    finally:
        os.close(directory)


def board_binding(slot):
    binding = slot["board"]
    host = binding["host_identity"]
    if (set(binding) != {"lineage", "host_identity", "note_uid"}
            or not isinstance(binding["lineage"], str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", binding["lineage"]) is None
            or not isinstance(binding["note_uid"], str) or not binding["note_uid"]
            or set(host) != {"project_uid", "path_sha256"} or not isinstance(host["project_uid"], str)
            or not host["project_uid"] or not isinstance(host["path_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", host["path_sha256"]) is None):
        raise ValueError("Configure a stable board lineage and saved Revit note identity.")
    return binding


def normalize_board(raw, slot):
    binding = board_binding(slot)
    record = read_saved_result(raw, ("query",), "Use a complete read-only issue board record.")
    note = record.get("note") if isinstance(record, dict) else None
    if (not isinstance(note, dict) or note.get("unique_id") != binding["note_uid"]
            or record.get("host_identity") != binding["host_identity"] or not isinstance(note.get("text"), str)):
        raise ValueError("The issue board does not match the configured Revit model and note.")
    board_scripts = Path(__file__).resolve().parents[2] / "am-issue-board" / "scripts"
    if str(board_scripts) not in sys.path:
        sys.path.insert(0, str(board_scripts))
    from issue_board import parse_board, text_sha1, unescape

    text = unescape(note["text"])
    if note.get("sha1") != text_sha1(text):
        raise ValueError("The issue board text does not match its saved read record.")
    parsed = parse_board(text)
    return {"header": {"title": parsed["header"]["title"]}, "warnings": parsed["warnings"],
            "issues": [{key: row[key] for key in ("number", "status", "title", "assignees", "severity", "findings",
                                                   "todos", "comments", "element_ids", "related")}
                       for row in parsed["issues"]]}


def validate_data(kind, data):
    if kind == "audit":
        collect_findings([data])
    elif kind == "preview":
        read_change_preview(data)
    elif kind == "board":
        render_board(data)
        if len({row["number"] for row in data["issues"]}) != len(data["issues"]):
            raise ValueError("The issue board contains duplicate issue numbers.")
    elif kind == "model":
        validate_model(data)
    elif kind == "document":
        validate_document(data)
    else:
        raise ValueError("Use a configured audit, preview, board, model or document artifact slot.")


def make_artifact(slot, generation, raw):
    if type(generation) is not int or not 1 <= generation <= 2**53 - 1:
        raise ValueError("Use a positive publication generation from the source run.")
    kind = slot["kind"]
    if kind == "audit":
        data = read_saved_result(raw, ("query",), "Use a complete read-only audit result.")
        if not isinstance(data, dict) or data.get("error"):
            raise ValueError("Use a complete read-only audit result.")
        keys = ("model", "timestamp", "revit_year", "metrics", "findings", "drawing_name", "source")
        data = {key: data[key] for key in keys if key in data}
    elif kind == "preview":
        preview = read_change_preview(raw)
        keys = ("kind", "title", "keyColumn", "columns", "changedColumns", "before", "after", "notes", "warnings",
                "review_only", "blocked")
        data = {key: preview[key] for key in keys if key in preview}
    elif kind == "board":
        data = normalize_board(raw, slot)
    elif kind == "model":
        validate_model(raw, normalize=True)
        data = raw
    elif kind == "document":
        data = normalize_document(raw)
    else:
        raise ValueError("Use a configured audit, preview, board, model or document artifact slot.")
    validate_data(kind, data)
    content = {"kind": kind, "data": data, "board": board_binding(slot) if kind == "board" else None}
    return {**content, "generation": generation, "digest": hashlib.sha256(canonical(content)).hexdigest()}


def read_slot(directory, name, slot):
    return validate_publication(read_json_file(name, slot_limit(slot), directory), slot)


def validate_publication(result, slot):
    if (not isinstance(result, dict) or set(result) != {"kind", "data", "board", "generation", "digest"}
            or result["kind"] != slot["kind"] or type(result["generation"]) is not int
            or not 1 <= result["generation"] <= 2**53 - 1):
        raise ValueError("The published artifact has an invalid format or generation.")
    expected_board = board_binding(slot) if result["kind"] == "board" else None
    content = {key: result[key] for key in ("kind", "data", "board")}
    if result["board"] != expected_board or result["digest"] != hashlib.sha256(canonical(content)).hexdigest():
        raise ValueError("The published artifact does not match the configured identity or digest.")
    validate_data(result["kind"], result["data"])
    return result


def read_published(root, slot):
    with slot_directory(root, slot) as (directory, name):
        return read_slot(directory, name, slot)


def review_targets(slot_name, artifact):
    kind, data = artifact["kind"], artifact["data"]
    if kind in ("model", "document"):
        return []
    if kind == "board":
        lineage = artifact["board"]["lineage"]
        return [f"board:{lineage}:{issue['number']}" for issue in data["issues"]]
    prefix = f"{kind}:{slot_name}:{artifact['digest']}"
    rows = collect_findings([data]) if kind == "audit" else pair_preview_rows(data)
    return [f"{prefix}:{index}" for index, _ in enumerate(rows)]


def publish(root, slot, generation, raw):
    import fcntl

    artifact = make_artifact(slot, generation, raw)
    payload = canonical(artifact)
    if len(payload) > slot_limit(slot):
        raise ValueError("The workspace publication exceeds its byte limit.")
    with slot_directory(root, slot) as (directory, name):
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        lock = os.open("." + name + ".publish.lock", flags, 0o600, dir_fd=directory)
        try:
            metadata = os.fstat(lock)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("Use a regular publication lock without links.")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                previous = read_slot(directory, name, slot)
            except FileNotFoundError:
                previous = None
            if previous is not None and generation <= previous["generation"]:
                if generation == previous["generation"] and artifact == previous:
                    return {"status": "already_published", "generation": generation, "digest": artifact["digest"]}
                raise ValueError("The publication generation is older or already identifies different content.")
            temporary = ".am-publish-" + uuid4().hex
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try:
                    os.unlink(temporary, dir_fd=directory)
                except FileNotFoundError:
                    pass
        finally:
            os.close(lock)
    return {"status": "published", "generation": generation, "digest": artifact["digest"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config")
    parser.add_argument("project")
    parser.add_argument("slot")
    parser.add_argument("generation", type=int)
    parser.add_argument("input")
    args = parser.parse_args()
    try:
        config = read_json_file(args.config, MAX_ARTIFACT_BYTES)
        project = config["projects"][args.project]
        slot = project["artifacts"][args.slot]
        limit = {"model": MAX_MODEL_BYTES, "document": MAX_DOCUMENT_INPUT}.get(slot["kind"], 3 * MAX_ARTIFACT_BYTES)
        raw = read_json_file(args.input, limit)
        print(json.dumps(publish(project["root"], slot, args.generation, raw)))
    except (AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError) as error:
        parser.exit(2, "Workspace publication stopped ({}). Check the configured slot, source and generation.\n"
                    .format(type(error).__name__))


if __name__ == "__main__":
    main()
