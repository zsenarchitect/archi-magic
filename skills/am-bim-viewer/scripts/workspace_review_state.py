"""Persist one office project's review state under an exclusive POSIX lock."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from uuid import uuid4


MAX_STATE_BYTES = 16 * 1024 * 1024
MAX_COMMENT_BYTES = 4096
STATUSES = {"open", "resolved", "waived"}


class RevisionConflict(ValueError):
    pass


class OperationConflict(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("The workspace JSON has duplicate fields.")
        result[key] = value
    return result


def operation_id_valid(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{16,80}", value) is not None


def actor_name(value):
    return isinstance(value, str) and bool(value.strip())


def comment_text_valid(value):
    return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= MAX_COMMENT_BYTES


def valid_comment(note):
    if (not isinstance(note, dict) or set(note) != {"actor", "text", "recorded_at"}
            or not actor_name(note["actor"]) or not comment_text_valid(note["text"])
            or not isinstance(note["recorded_at"], str)):
        return False
    try:
        timestamp = datetime.fromisoformat(note["recorded_at"])
    except ValueError:
        return False
    return timestamp.tzinfo is not None


def write_state(path, state, replace):
    raw = canonical(state)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("The project sidecar exceeds its storage limit.")
    path = Path(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    pending = ".am-issues-" + uuid4().hex
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
        descriptor = os.open(pending, flags, 0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(pending, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.link(pending, path.name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        finally:
            try:
                os.unlink(pending, dir_fd=directory)
            except FileNotFoundError:
                pass
        os.fsync(directory)
    finally:
        os.close(directory)


class ReviewState:
    def __init__(self, root, project, members, targets):
        if not isinstance(project, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", project) is None:
            raise ValueError("Use a configured project slug.")
        if any(not actor_name(actor) or role not in ("viewer", "editor")
               for actor, role in members.items()):
            raise ValueError("Use a configured project roster.")
        self.root = Path(root)
        self.path = self.root / ".am-issues.json"
        self.project = project
        self.members = dict(members)
        self.targets = frozenset(targets)
        self._artifacts = {}
        self._thread_lock = RLock()
        self._file_lock = None

    def __enter__(self):
        import fcntl

        if self._file_lock is not None:
            raise RuntimeError("The project state is already open.")
        descriptor = os.open(self.root / ".am-issues.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("The project lock must be a regular file without links.")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._file_lock = descriptor
        except BaseException:
            os.close(descriptor)
            raise
        return self

    def __exit__(self, *_):
        with self._thread_lock:
            os.close(self._file_lock)
            self._file_lock = None
            self._artifacts.clear()

    def _authorize(self, actor, edit=False):
        if self._file_lock is None:
            raise RuntimeError("Open the project state before use.")
        role = self.members.get(actor)
        if role is None or (edit and role != "editor"):
            raise PermissionError("This account cannot access the requested project operation.")

    def _load(self):
        try:
            return self._read_file(self.path)
        except FileNotFoundError:
            return {"version": 1, "project": self.project, "revision": 0, "annotations": {}, "operations": {},
                    "artifacts": {}}

    def _read_file(self, path):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("The project sidecar must be a regular file.")
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ValueError("The project sidecar exceeds its storage limit.")
        data = json.loads(raw, object_pairs_hook=unique_object)
        if isinstance(data, dict):
            data.setdefault("artifacts", {})
        if (not isinstance(data, dict) or set(data) != {
                "version", "project", "revision", "annotations", "operations", "artifacts"}
                or type(data["version"]) is not int or data["version"] != 1 or data["project"] != self.project
                or type(data["revision"]) is not int or data["revision"] < 0
                or any(not isinstance(data[field], dict) for field in ("annotations", "operations", "artifacts"))):
            raise ValueError("The project sidecar has an invalid identity or format.")
        if any(not isinstance(entry, dict) or set(entry) != {"generation", "digest"}
               or type(entry["generation"]) is not int or not 1 <= entry["generation"] <= 2**53 - 1
               or not isinstance(entry["digest"], str) or re.fullmatch(r"[0-9a-f]{64}", entry["digest"]) is None
               for entry in data["artifacts"].values()):
            raise ValueError("The project sidecar has invalid artifact generations.")
        return data

    def _store(self, state):
        write_state(self.path, state, replace=True)

    def export_snapshot(self, destination):
        with self._thread_lock:
            if self._file_lock is None:
                raise RuntimeError("Open the project state before maintenance.")
            state = self._read_file(self.path)
            self._validate_snapshot(state)
            write_state(destination, state, replace=False)
            return self._etag(state)

    def import_snapshot(self, source):
        with self._thread_lock:
            if self._file_lock is None:
                raise RuntimeError("Open the project state before maintenance.")
            state = self._read_file(source)
            self._validate_snapshot(state)
            write_state(self.path, state, replace=False)
            return self._etag(state)

    def _validate_snapshot(self, state):
        annotations = state["annotations"]
        for annotation in annotations.values():
            if (not isinstance(annotation, dict) or set(annotation) != {"status", "assignee", "comments"}
                    or not isinstance(annotation["status"], str) or annotation["status"] not in STATUSES
                    or (annotation["assignee"] is not None and not actor_name(annotation["assignee"]))
                    or not isinstance(annotation["comments"], list)
                    or not all(valid_comment(note) for note in annotation["comments"])):
                raise ValueError("The project snapshot contains invalid annotations.")
        for actor, operations in state["operations"].items():
            if not actor_name(actor) or not isinstance(operations, dict):
                raise ValueError("The project snapshot contains invalid operation history.")
            for operation, record in operations.items():
                if (not operation_id_valid(operation)
                        or not isinstance(record, dict) or set(record) != {"fingerprint", "result"}
                        or not isinstance(record["fingerprint"], str)
                        or re.fullmatch(r"[0-9a-f]{64}", record["fingerprint"]) is None):
                    raise ValueError("The project snapshot contains an invalid operation record.")
                result = record["result"]
                if (not isinstance(result, dict) or set(result) != {"etag", "target"}
                        or not isinstance(result["etag"], str) or not isinstance(result["target"], str)
                        or result["target"] not in annotations):
                    raise ValueError("The project snapshot contains an invalid operation receipt.")
                match = re.fullmatch(r'"' + self.project + r'-([1-9][0-9]*)"', result["etag"])
                if match is None or int(match[1]) > state["revision"]:
                    raise ValueError("The project snapshot contains an invalid operation revision.")

    def _etag(self, state):
        return '"{}-{}"'.format(self.project, state["revision"])

    def read(self, actor):
        with self._thread_lock:
            self._authorize(actor)
            state = self._load()
            return {"etag": self._etag(state), "annotations": state["annotations"]}

    def read_workspace(self, actor):
        with self._thread_lock:
            self._authorize(actor)
            state = self._load()
            return {"etag": self._etag(state), "annotations": state["annotations"],
                    "targets": sorted(self.targets)}, self._artifacts

    def read_audits(self, actor, slots):
        with self._thread_lock:
            self._authorize(actor)
            revision = self._load()["revision"]
            return revision, [(slot, self._artifacts.get(slot, {"freshness": "unavailable"})) for slot in slots]

    def read_artifact(self, actor, slot):
        with self._thread_lock:
            self._authorize(actor)
            return self._artifacts.get(slot, {"freshness": "unavailable"})

    def refresh_artifacts(self, slots, read_slot):
        """Commit new generations and their review targets in one project operation."""
        with self._thread_lock:
            if self._file_lock is None:
                raise RuntimeError("Open the project state before use.")
            state = self._load()
            updated = {}
            for name, slot in slots.items():
                artifact, targets = read_slot(self.root, name, slot)
                previous = state["artifacts"].get(name)
                current = self._artifacts.get(name, {"freshness": "unavailable"})
                if artifact is not None:
                    generation, digest = artifact["generation"], artifact["digest"]
                    if previous is not None and (generation < previous["generation"] or (
                            generation == previous["generation"] and digest != previous["digest"])):
                        artifact = None
                if artifact is None:
                    updated[name] = dict(current, freshness="stale") if "data" in current else current
                    continue
                state["artifacts"][name] = {"generation": generation, "digest": digest}
                updated[name] = {"freshness": "ready", "kind": artifact["kind"], "generation": generation,
                                 "digest": digest, "data": artifact["data"], "targets": targets}
            changed = any(updated[name] != self._artifacts.get(name, {"freshness": "unavailable"}) for name in updated)
            if not changed:
                return None
            state["revision"] += 1
            self._store(state)
            self._artifacts = updated
            return self._etag(state)

    def update(self, actor, target, operation_id, if_match, patch):
        result, _ = self.update_with_outcome(actor, target, operation_id, if_match, patch)
        return result

    def update_with_outcome(self, actor, target, operation_id, if_match, patch):
        with self._thread_lock:
            self._authorize(actor, edit=True)
            if not operation_id_valid(operation_id):
                raise ValueError("Use an operation ID with 16 to 80 letters, digits, hyphens or underscores.")
            if not isinstance(target, str) or not isinstance(patch, dict) or not patch or set(patch) - {
                    "status", "assignee", "comment"}:
                raise ValueError("Use an exact review target and a supported annotation change.")
            fingerprint = hashlib.sha256(canonical([target, patch])).hexdigest()
            state = self._load()
            operations = state["operations"].setdefault(actor, {})
            prior = operations.get(operation_id)
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise OperationConflict("The operation ID already identifies a different change.")
                return prior["result"], False
            if if_match != self._etag(state):
                raise RevisionConflict("The project review changed. Refresh before editing.")
            if target not in self.targets and not any(
                    artifact["freshness"] == "ready" and target in artifact["targets"]
                    for artifact in self._artifacts.values()):
                raise ValueError("The review target is unavailable or retired.")

            status, assignee, comment = (patch.get(key) for key in ("status", "assignee", "comment"))
            if "status" in patch and (not isinstance(status, str) or status not in STATUSES):
                raise ValueError("Use an open, resolved or waived review status.")
            if "assignee" in patch and assignee is not None and (not isinstance(assignee, str) or assignee not in self.members):
                raise ValueError("Assign a configured project member.")
            if "comment" in patch and not comment_text_valid(comment):
                raise ValueError("Use a nonempty comment of at most 4 KiB.")

            item = state["annotations"].setdefault(target, {"status": "open", "assignee": None, "comments": []})
            item.update({key: patch[key] for key in ("status", "assignee") if key in patch})
            if "comment" in patch:
                item["comments"].append({"actor": actor, "text": comment, "recorded_at": datetime.now(timezone.utc).isoformat()})
            state["revision"] += 1
            result = {"etag": self._etag(state), "target": target}
            operations[operation_id] = {"fingerprint": fingerprint, "result": result}
            self._store(state)
            return result, True
