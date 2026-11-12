"""Compare reviewed architectural and structural elements and preview issue additions."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import sys

import board_cli
import issue_board as ib

LIMIT = 2 * 1024 * 1024
MARKER = "Structural comparison key: "
CATEGORIES = ("column", "wall", "slab")


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate input key.")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Invalid numeric constant.")


def finite_float(token):
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("Invalid numeric evidence.")
    return value


def read_input(path):
    if str(path).startswith(("//", "\\\\")) or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("Expected regular local file.")
    with path.open("rb") as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Input exceeds 2 MiB.")
    return (json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_json,
                       parse_constant=reject_constant, parse_float=finite_float),
            hashlib.sha256(raw).hexdigest())


def checked_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) > 2**63 - 1:
        raise ValueError("Invalid decimal element ID.")
    return value


def uid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", value):
        raise ValueError("Invalid identity token.")
    return value


def digest(value, length=64):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]+", value) or len(value) != length:
        raise ValueError("Invalid identity digest.")
    return value


def identity(value):
    if not isinstance(value, dict) or set(value) != {"project_uid", "path_sha256"}:
        raise ValueError("Invalid saved document identity.")
    return uid(value["project_uid"]), digest(value["path_sha256"])


def number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError("Out-of-range numeric evidence.")
    return value


def bounded_list(value, count):
    if not isinstance(value, list) or len(value) > count:
        raise ValueError("Input list exceeds allowed count.")
    return value


def inventory(rows):
    items = {}
    uids = set()
    for item in bounded_list(rows, 300):
        element_id = checked_id(item["id"])
        unique = uid(item["uid"])
        if item["category"] not in CATEGORIES or element_id in items or unique in uids:
            raise ValueError("Duplicate or unsupported inventory element.")
        a = bounded_list(item["min"], 3)
        b = bounded_list(item["max"], 3)
        if len(a) != 3 or len(b) != 3:
            raise ValueError("Envelope must have three axes.")
        low = [number(n, -1e6, 1e6) for n in a]
        high = [number(n, -1e6, 1e6) for n in b]
        if not all(0 < y - x <= 10000 for x, y in zip(low, high)):
            raise ValueError("Invalid envelope size.")
        items[element_id] = (unique, item["category"], low, high)
        uids.add(unique)
    return items


def pair_evidence(host, link):
    shifts = [((c + d) - (a + b)) * 152.4 for a, b, c, d in zip(host[2], host[3], link[2], link[3])]
    extents = [((d - c) - (b - a)) * 304.8 for a, b, c, d in zip(host[2], host[3], link[2], link[3])]
    return math.sqrt(sum(delta * delta for delta in shifts)), extents


def compare(snapshot, review):
    if (snapshot["kind"] != "structural-geometry-snapshot"
            or snapshot["frame"] != "revit_host_internal_feet"):
        raise ValueError("Expected a structural geometry snapshot in host internal feet.")
    scope = snapshot["scope"]
    if not isinstance(scope, str) or not scope.strip() or len(scope) > 160:
        raise ValueError("Missing reviewed inventory scope.")
    model_pair = (identity(snapshot["host_identity"]), identity(snapshot["link_identity"]),
                  uid(snapshot["link_instance_uid"]))
    offset_tol = number(review["center_tolerance_mm"], 0.01, 10000)
    extent_tol = number(review["extent_tolerance_mm"], 0.01, 10000)
    host = inventory(snapshot["host"])
    link = inventory(snapshot["link"])
    if not host and not link:
        raise ValueError("Inventory is empty.")
    unused_host, unused_link = set(host), set(link)
    compared = []
    for row in bounded_list(review["dispositions"], 600):
        kind = row["kind"]
        host_id, link_id = row.get("host"), row.get("link")
        for element_id, unused in ((host_id, unused_host), (link_id, unused_link)):
            if element_id is not None:
                unused.remove(checked_id(element_id))
        left, right = host.get(host_id), link.get(link_id)
        record = {"host": host_id, "link": link_id, "disposition": kind, "findings": []}
        if kind == "pair" and left and right and left[1] == right[1]:
            shift, extents = pair_evidence(left, right)
            record.update(category=left[1], center_shift_mm=shift, extent_delta_mm=extents)
            if shift > offset_tol:
                record["findings"].append("center_shift")
            if max(abs(n) for n in extents) > extent_tol:
                record["findings"].append("envelope_extent")
        elif kind in ("missing", "defer") and ((left is None) != (right is None)):
            record["category"] = (left or right)[1]
            if kind == "missing":
                record["findings"].append("missing_link" if left else "missing_host")
        else:
            raise ValueError("Invalid reviewed pairing or disposition.")
        record["keys"] = {}
        for finding in record["findings"]:
            parts = ("structural-v1", model_pair, left[0] if left else None, right[0] if right else None, finding)
            record["keys"][finding] = hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("ascii")).hexdigest()
        compared.append(record)
    if unused_host or unused_link:
        raise ValueError("Every inventory element needs a reviewed disposition.")
    return compared


def read_board(path, snapshot, archive):
    source, _ = read_input(path)
    host = source["host_identity"]
    if identity(host) != identity(snapshot["host_identity"]):
        raise ValueError("Board is from a different saved project.")
    note = source["note"]
    target = {"id": checked_id(str(note["id"])), "unique_id": uid(note["unique_id"]),
              "sha1": digest(note["sha1"], 40)}
    body, formats = board_cli.decode_note(source)
    if ib.text_sha1(body) != target["sha1"]:
        raise ValueError("Board text disagrees with its read digest.")
    if formats is not None:
        for fmt in bounded_list(formats, 10000):
            if (type(fmt["indent"]) is not int or not 0 <= fmt["indent"] <= 20
                    or str(fmt["list"]).lower() not in ("none", "bullet")):
                raise ValueError("Unsupported board paragraph format.")
    board = ib.parse_board(body, formats)
    header, rows = board["header"], board["issues"]
    numbers = {row["number"] for row in rows}
    if (header["archive"] != archive or not header["title"] or len(numbers) != len(rows)
            or (not archive and (header["next_number"] or 0) <= max(numbers, default=0))):
        raise ValueError("Invalid board role, title, or numbering.")
    seen = set()
    for row in rows:
        for finding in row["findings"]:
            if finding.startswith(MARKER):
                key = digest(finding[len(MARKER):])
                if key in seen:
                    raise ValueError("Duplicate structural provenance.")
                seen.add(key)
    return body, formats, rows, seen, target


def board_preview(path, archive, snapshot, compared):
    body, formats, original, seen, active = read_board(path, snapshot, False)
    archive_target = None
    if archive:
        _, _, _, archived, archive_target = read_board(archive, snapshot, True)
        if seen & archived or archive_target["id"] == active["id"]:
            raise ValueError("Board and archive overlap.")
        seen.update(archived)
    ops, skipped = [], []
    for item in compared:
        for finding, key in item["keys"].items():
            if key in seen:
                skipped.append(key)
                continue
            titles = {"center_shift": "sampled envelope center shift",
                      "envelope_extent": "sampled envelope extent difference",
                      "missing_link": "reviewed missing linked counterpart",
                      "missing_host": "reviewed missing host counterpart"}
            description = titles[finding] + "."
            if finding == "center_shift":
                description = "Sampled envelope center shift %.3f mm." % item["center_shift_mm"]
            elif finding == "envelope_extent":
                description = "Sampled envelope extent deltas %.3f, %.3f, %.3f mm." % tuple(item["extent_delta_mm"])
            elements = "Host element %s. Linked element %s." % (item["host"] or "missing", item["link"] or "missing")
            issue = {"title": "Structural " + item["category"] + " " + titles[finding],
                     "status": "OPEN", "findings": [MARKER + key, description, elements]}
            ops.append({"op": "add", "issue": issue})
    if not ops:
        return None, skipped
    if len(ops) > 100:
        raise ValueError("Use smaller batches of at most 100 board additions.")
    plan = ib.plan_batch(body, ops, formats)
    after = ib.parse_board(plan["final_text"])["issues"]
    by_number = {row["number"]: row for row in after}
    if (len(after) != len(original) + len(ops) or len(after) != len(by_number)
            or any(by_number.get(row["number"]) != row for row in original)):
        raise ValueError("Unexpected existing board content change.")
    for operation, request in zip(plan["operations"], ops):
        row = by_number[int(operation.removeprefix("add #"))]
        issue = request["issue"]
        if (any(row[field] != issue[field] for field in ("title", "status", "findings"))
                or row["assignees"] != ["@TBD"] or row["parent"] is not None
                or any(row[field] for field in ("element_ids", "related", "todos", "comments", "severity"))):
            raise ValueError("Unexpected new board content.")
    plan["structural_target"] = {"host_identity": snapshot["host_identity"], "active": active,
                                 "archive": archive_target}
    return plan, skipped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("review", type=Path)
    parser.add_argument("--board", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.archive and not args.board:
            raise ValueError("An archive needs an active board.")
        snapshot, snapshot_sha = read_input(args.snapshot)
        review, review_sha = read_input(args.review)
        if review["snapshot_sha256"] != snapshot_sha:
            raise ValueError("Review does not match snapshot.")
        compared = compare(snapshot, review)
        plan, skipped = board_preview(args.board, args.archive, snapshot, compared) if args.board else (None, [])
        report = {"kind": "structural-comparison-preview", "snapshot_sha256": snapshot_sha,
                  "review_sha256": review_sha, "scope": snapshot["scope"], "elements": compared,
                  "plan": plan, "skipped_keys": skipped,
                  "warnings": ["Archive note not checked for duplicates."] if args.board and not args.archive else []}
        output = json.dumps(report, allow_nan=False, ensure_ascii=True, indent=2)
        if len(output) > LIMIT:
            raise ValueError("Preview exceeds 2 MiB.")
        print(output)
    except (OSError, TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError, ib.BoardError):
        print("Structural comparison failed. Check file size, identity, geometry, mappings, or board data.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
