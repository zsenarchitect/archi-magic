# -*- coding: utf-8 -*-
"""Build a review-only view filter transfer from a complete native inventory."""
from __future__ import print_function

import argparse
import json
import re
import sys

from view_duplicate_preview import element_id, read_payload, read_record, snapshot_digest

TRANSACTION_NAME = "Test reviewed view filter transfer"
RUN_ID = re.compile(r"^[0-9a-f]{12}\Z")
COLUMNS = ["Row", "View", "Sheet", "Filter", "Action", "Priority", "Visible", "Enabled", "Graphics"]


def filter_stack(view):
    rows = view["filters"]
    result = {element_id(row["id"]): row for row in rows}
    if len(result) != len(rows) or len(rows) > 100:
        raise ValueError("A view filter inventory is incomplete or has repeated filters.")
    return result


def graphic_text(row):
    if row is None:
        return None
    return json.dumps(row["graphics"], ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def preview_rows(view, original, desired, source_ids):
    old = {row["id"]: (index, row) for index, row in enumerate(original, 1)}
    placement = ", ".join(item[2] for item in view["placements"])
    for index, row in enumerate(desired, 1):
        filter_id = row["id"]
        priority, previous = old.get(filter_id, (None, None))
        action = ("Add" if previous is None else "Update" if previous != row
                  else "Move" if priority != index else
                  "Already matches" if filter_id in source_ids else "Retain")
        key = view["id"] + ":" + filter_id
        prefix = [key, view["name"], placement, row["name"], action]
        before = prefix + ([priority, previous["visible"], previous["enabled"], graphic_text(previous)]
                           if previous is not None else [None, None, None, None])
        yield before, prefix + [index, row["visible"], row["enabled"], graphic_text(row)]


def compute(snapshot):
    if snapshot["kind"] != "view-filter-transfer-inventory":
        raise ValueError("Expected a native filter transfer inventory with verified view scope.")
    basis = snapshot_digest(snapshot)
    scope, views = snapshot["scope"], snapshot["views"]
    source_id = element_id(scope["source_id"])
    source_authority = element_id(scope["source_authority_id"])
    source = views[source_id]
    if source["authority_id"] != source_authority or source["filters"] != views[source_authority]["filters"]:
        raise ValueError("The source filter authority is inconsistent.")
    source_filters = filter_stack(source)
    before, after, warnings, steps, ownership = [], [], [], [], set()
    seen = {source_id, source_authority}

    for target in scope["targets"]:
        recipient_id = element_id(target["recipient_id"])
        affected = target["affected_ids"]
        if (recipient_id not in affected or element_id(target["requested_id"]) not in affected
                or len(set(affected)) != len(affected) or seen.intersection(affected)):
            raise ValueError("The transfer contains overlapping or incomplete view scope.")
        seen.update(affected)
        recipient = views[recipient_id]
        current = recipient["filters"]
        filter_stack(recipient)
        desired = source["filters"] + [row for row in current if row["id"] not in source_filters]
        if len(desired) > 100:
            raise ValueError("The merged filter stack exceeds 100 filters in a view.")
        changed = desired != current
        if changed:
            steps.append({"recipient_id": recipient_id, "affected_ids": affected,
                          "before": current, "after": desired})
            ownership.update(affected)
        for view_id in affected:
            view = views[element_id(view_id)]
            if view["id"] != view_id or view["authority_id"] != recipient_id or view["filters"] != current:
                raise ValueError("The effective graphics differ inside a target scope.")
            if changed and (not view["writable"] or (view["placements"] and not scope["allow_placed"])):
                reason = ("Worksharing or model update state blocks this view." if not view["writable"]
                          else "Approve the listed sheet placement before transferring filters.")
                warnings.append({"row": view_id + ":" + desired[0]["id"],
                                 "text": "%s [%s] %s" % (view["name"], view_id, reason), "blocking": True})
            for old, new in preview_rows(view, current, desired, source_filters):
                before.append(old)
                after.append(new)

    if seen != set(views):
        raise ValueError("The filter transfer inventory contains unreviewed views.")
    ownership_ids = sorted(ownership, key=int)
    blocked = bool(warnings)
    plan = {"kind": "view-filter-transfer-plan", "basis_sha256": basis, "document": snapshot["document"],
            "scope": scope, "blocked": blocked, "steps": [] if blocked else steps,
            "ownership_ids": ownership_ids}
    display = {"kind": "change-preview", "recipe": "view-filter-transfer", "title": "Copy view filters",
               "basis_sha256": basis, "keyColumn": "Row", "columns": COLUMNS,
               "changedColumns": ["Priority", "Visible", "Enabled", "Graphics"],
               "before": before, "after": after, "warnings": warnings, "blocked": blocked,
               "ownership_ids": ownership_ids, "steps": [], "review_only": True}
    snapshot_digest(plan)
    snapshot_digest(display)
    return plan, display


def checked_record(record, mode):
    record = record.get("response", record)
    run_id = record["run_id"]
    if (not RUN_ID.match(run_id) or record["mode"] != mode or record["status"] != "ok"
            or record["decision"] != "rolled_back" or record.get("approval") is not None
            or type(record["exit_code"]) is not int or record["exit_code"] != 0
            or record["result_truncated"] is not False or record["error"] is not None
            or any(record[field] != [] for field in ("failures", "dialogs", "blocked", "warnings"))
            or record.get("unclosed_documents")):
        raise ValueError("The full pyRevit record does not establish a clean %s rollback." % mode)
    return record


def checked_changes(record, expected):
    changes = record["changes"]
    if (any(type(changes[field]) is not int or changes[field] != count for field, count in (
            ("added_count", 0), ("deleted_count", 0), ("modified_count", len(expected))))
            or changes["added"] != [] or changes["deleted"] != [] or changes.get("other_documents")):
        raise ValueError("The bridge reported unreviewed additions, deletions or document changes.")
    modified = sorted((element_id(str(row["id"])) for row in changes["modified"]), key=int)
    category_counts = changes["by_category"].values()
    transactions = [TRANSACTION_NAME] if expected else []
    if (modified != expected or changes["transactions"] != transactions
            or any(type(count) is not int or count <= 0 for count in category_counts)
            or sum(category_counts) != len(expected)):
        raise ValueError("The bridge change record disagrees with the complete recipe telemetry.")


def checked_ownership_delta(delta, allowed, baseline):
    if not isinstance(delta, list):
        raise ValueError("The rollback receipt has invalid ownership data.")
    changed = set()
    for row in delta:
        view_id = element_id(row["view_id"])
        if (view_id not in allowed or view_id in changed or row["before"] != baseline[view_id]["ownership"]
                or row["before"] == row["after"]
                or set(row) != {"view_id", "before", "after"}):
            raise ValueError("The rollback receipt has an unreviewed ownership change.")
        changed.add(view_id)


def verify_rollback(plan, original_record, probe_record, fresh_record):
    original = checked_record(original_record, "query")
    probe = checked_record(probe_record, "dry_run")
    fresh = checked_record(fresh_record, "query")
    if len({record["run_id"] for record in (original, probe, fresh)}) != 3:
        raise ValueError("Supply three distinct pyRevit run records.")
    checked_changes(original, [])
    checked_changes(fresh, [])
    baseline = read_payload(original, "inventory_json")
    expected_plan, _ = compute(baseline)
    if expected_plan != plan or plan["blocked"] or not plan["steps"]:
        raise ValueError("The original record does not reproduce the reviewed transfer plan.")
    receipt = probe["result"]
    if (receipt["status"] != "trial_rolled_back" or receipt["basis_sha256"] != plan["basis_sha256"]
            or receipt["document"] != plan["document"]):
        raise ValueError("The rollback probe receipt does not match the reviewed transfer.")
    allowed = set(plan["ownership_ids"])
    required = {step["recipient_id"] for step in plan["steps"]}
    changes = receipt["changes"]
    modified = changes["modified"]
    if (changes["added"] != [] or changes["deleted"] != [] or not isinstance(modified, list)
            or modified != sorted(set(modified), key=int) or not required.issubset(modified)
            or not set(modified).issubset(allowed)):
        raise ValueError("The rollback probe did not report the reviewed recipient change set.")
    checked_changes(probe, modified)
    checked_ownership_delta(receipt["trial_ownership_changes"], allowed, baseline["views"])
    checked_ownership_delta(receipt["remaining_ownership_changes"], allowed, baseline["views"])

    observed = read_payload(fresh, "inventory_json")
    if set(observed["views"]) != set(baseline["views"]):
        raise ValueError("The independent query returned a different view scope.")
    remaining = []
    for view_id in sorted(baseline["views"], key=int):
        before, after = baseline["views"][view_id], observed["views"][view_id]
        if before["ownership"] != after["ownership"]:
            remaining.append({"view_id": view_id, "before": before["ownership"], "after": after["ownership"]})
            after.update(ownership=before["ownership"], writable=before["writable"])
    if observed != baseline:
        raise ValueError("The independent Revit query differs from the original filter and view state.")
    if remaining != receipt["remaining_ownership_changes"]:
        raise ValueError("The independent query disagrees with the reported remaining ownership changes.")
    return {"status": "ownership_reconciliation_required" if remaining else "verified_fixture_rollback",
            "basis_sha256": plan["basis_sha256"], "document": plan["document"],
            "original_run_id": original["run_id"], "probe_run_id": probe["run_id"],
            "fresh_run_id": fresh["run_id"], "modified_view_ids": modified, "remaining_ownership_changes": remaining}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_record", help="Full Revit runtime read record")
    parser.add_argument("--verify-rollback", nargs=3, metavar=("PLAN", "PROBE", "FRESH"),
                        help="Verify the plan, full dry-run record, and independent post-rollback query")
    args = parser.parse_args()
    try:
        record = read_record(args.runtime_record)
        if args.verify_rollback:
            plan_file, probe_file, fresh_file = args.verify_rollback
            plan = read_record(plan_file)
            report = verify_rollback(plan.get("plan", plan), record, read_record(probe_file), read_record(fresh_file))
            print(json.dumps(report, ensure_ascii=True, indent=2))
            return 1 if report["remaining_ownership_changes"] else 0
        snapshot = read_payload(record.get("response", record), "inventory_json")
        plan, display = compute(snapshot)
        print(json.dumps({"plan": plan, "preview": display}, ensure_ascii=True, indent=2))
        return 1 if plan["blocked"] else 0
    except (ValueError, KeyError, TypeError, AttributeError, IOError) as error:
        print("View filter transfer stopped: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
