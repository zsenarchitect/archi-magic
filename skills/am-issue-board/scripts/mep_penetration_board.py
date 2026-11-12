"""Prepare a review-only issue-board handoff for selected linked MEP crossings."""

import argparse
import json
from pathlib import Path
import sys

import issue_board as board
from mep_penetration_preview import fingerprint, preview, vector
from structural_compare_cli import bounded_list, checked_id, digest, read_board, read_input

MARKER = "MEP penetration key: "
TITLES = {
    "proposed": "Linked MEP crossing needs an opening",
    "out_of_date": "Tracked MEP opening is out of date",
}


def matched_numbers(rows):
    return {finding[len(MARKER):]: issue["number"]
            for issue in rows for finding in issue["findings"] if finding.startswith(MARKER)}


def context_label(raw, field):
    if not isinstance(raw, str) or not raw.isprintable() or len(raw) > 160 or board.ID_RE.search(raw):
        raise ValueError(f"The {field} cannot be safely represented in the board.")
    return raw


def preview_board(snapshot, keys, active_path, archive_path=None):
    report = preview(snapshot)
    keys = [digest(key) for key in bounded_list(keys, 100)]
    if not report["complete"] or not keys or len(keys) != len(set(keys)):
        raise ValueError("Review a complete scan and one to one hundred distinct findings.")
    findings = {row["crossing_key"]: row for row in report["findings"]}
    body, formats, original, seen, active = read_board(active_path, snapshot, False, MARKER)
    active_numbers = matched_numbers(original)
    archive_target = None
    archived_numbers = {}
    if archive_path:
        _, _, archived, archived_seen, archive_target = read_board(archive_path, snapshot, True, MARKER)
        if seen & archived_seen or archive_target["id"] == active["id"]:
            raise ValueError("The active board and archive overlap.")
        archived_numbers = matched_numbers(archived)
    ops, existing, host_ids = [], [], []
    for key in keys:
        item = findings.get(key)
        if item is None or item["status"] not in TITLES:
            raise ValueError("A selected crossing does not have a verified opening finding.")
        if key in active_numbers or key in archived_numbers:
            archived = key in archived_numbers
            existing.append({"crossing_key": key, "number": (archived_numbers if archived else active_numbers)[key],
                             "archive": archived, "observed_status": item["status"]})
            continue
        host, linked = checked_id(item["host_id"]), checked_id(item["linked_run_id"])
        location = vector(item["location"], 3)
        system = context_label(item["system"], "MEP system name") if item["system"] is not None else "unassigned"
        details = [MARKER + key,
                   f"Host id{host}. Linked run {linked}. System {system}.",
                   "Host XYZ in internal feet at the %s is %.6f, %.6f, %.6f." % (item["location_basis"], *location)]
        if item["fire_rating"] is not None:
            rating = context_label(item["fire_rating"], "host fire rating")
            details.append(f"Host fire rating recorded in Revit is {rating}.")
        host_ids.append(int(host))
        ops.append({"op": "add", "issue": {"title": TITLES[item["status"]], "status": "OPEN", "findings": details}})
    result = {"kind": "mep-penetration-board-preview", "evidence_sha256": fingerprint(snapshot),
              "existing": existing, "additions": len(ops), "plan": None}
    if not ops:
        return result

    plan = board.plan_batch(body, ops, formats)
    after = board.parse_board(plan["final_text"])["issues"]
    by_number = {row["number"]: row for row in after}
    if (len(after) != len(original) + len(ops) or len(by_number) != len(after)
            or any(by_number.get(row["number"]) != row for row in original)):
        raise ValueError("The preview changed existing board entries.")
    for operation, request, host_id in zip(plan["operations"], ops, host_ids):
        added = by_number[int(operation.removeprefix("add #"))]
        expected = request["issue"]
        if (any(added[key] != expected[key] for key in expected)
                or added["element_ids"] != [host_id] or added["assignees"] != ["@TBD"]
                or any(added[key] for key in ("parent", "related", "todos", "comments", "severity"))):
            raise ValueError("The new entry differs from the reviewed MEP finding.")
    plan["mep_penetration_target"] = {"host_identity": snapshot["host_identity"],
                                      "active": active, "archive": archive_target}
    result["plan"] = plan
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--crossing", action="append", required=True)
    args = parser.parse_args()
    try:
        result = preview_board(read_input(args.evidence)[0], args.crossing, args.board, args.archive)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError, board.BoardError) as error:
        print(f"MEP issue-board preview blocked. {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
