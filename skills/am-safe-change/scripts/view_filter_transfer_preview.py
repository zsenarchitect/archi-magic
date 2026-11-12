# -*- coding: utf-8 -*-
"""Build a review-only view filter transfer from a complete native inventory."""
from __future__ import print_function

import argparse
import json
import sys

from view_duplicate_preview import MAX_BYTES, element_id, read_payload, read_record, snapshot_digest

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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_record", help="Full Revit runtime read record")
    args = parser.parse_args()
    try:
        snapshot = read_payload(read_record(args.runtime_record), "inventory_json")
        plan, display = compute(snapshot)
        print(json.dumps({"plan": plan, "preview": display}, ensure_ascii=True, indent=2))
        return 1 if plan["blocked"] else 0
    except (ValueError, KeyError, TypeError, AttributeError, IOError) as error:
        print("View filter transfer stopped: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
