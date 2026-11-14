"""Report configured hides and compute a reviewed visibility change."""
from __future__ import print_function

import argparse
import copy
import io
import json
import os
import sys

from focus_view_preview import load, read_basis


def report(snapshot):
    categories = {row["id"]: row for row in snapshot["categories"]}
    filters = {row["id"]: row for row in snapshot["filters"]}
    rows = []
    for element in snapshot["elements"]:
        reasons = []
        if element["direct"] is True:
            reasons.append({"kind": "element", "origin": "view"})
        category_chain = [categories[key] for key in element["category_path"]]
        for category in category_chain:
            if category["hidden"] is True:
                reasons.append({"kind": "category", "origin": category["origin"], "id": category["id"]})
        for key in element["matched_filters"]:
            item = filters[key]
            if item["hidden"] is True:
                reasons.append({"kind": "filter", "origin": item["origin"], "id": key})
        unknown = (element["direct"] is None or any(item["hidden"] is None for item in category_chain)
                   or any(filters[key]["hidden"] is None for key in element["matched_filters"])
                   or bool(element["unknown_filters"]) or any(item["origin"] == "unknown" for item in reasons))
        rows.append({"id": element["id"], "category": category_chain[0]["id"], "reasons": reasons,
                     "matching_filter_order": element["matched_filters"], "incomplete_evidence": unknown})
    return {"view": snapshot["view"], "scope": snapshot["scope"], "ignored": snapshot["ignored"],
            "rows": rows, "whole_view_impact": snapshot["impact"],
            "note": "Configured hide evidence only. Other visibility mechanisms and rendered visibility are not assessed."}


def expected_after(snapshot, kind, ids):
    expected = copy.deepcopy(snapshot)
    collection, flag = {"element": ("elements", "direct"), "category": ("categories", "hidden"),
                        "filter": ("filters", "hidden")}[kind]
    for row in expected[collection]:
        if row["id"] in ids:
            row[flag] = False
    return expected


def compute(snapshot, intent):
    if snapshot["kind"] != "hidden-elements-snapshot" or not snapshot["view"]["writable"]:
        raise ValueError("The reviewed active view cannot be changed.")
    ids = intent["ids"]
    kind = intent["kind"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids)
            or any(not isinstance(key, (type(u""), str)) for key in ids)):
        raise ValueError("Choose one to 100 distinct reviewed IDs.")
    if kind == "element":
        if snapshot["scope"]["whole"] is not None:
            raise ValueError("Read a direct-only scope for element unhide.")
        rows = {item["id"]: item for item in snapshot["elements"]}
        if any(key not in rows or rows[key]["direct"] is not True for key in ids):
            raise ValueError("Every chosen element must be directly hidden in this view.")
        warnings = []
    elif kind in ("category", "filter"):
        if (len(ids) != 1 or intent.get("allow_whole_view") is not True
                or snapshot["scope"]["whole"] != {"kind": kind, "id": ids[0]}
                or snapshot["impact"] is None):
            raise ValueError("Explicitly review and authorize the exact whole-view scope first.")
        items = snapshot["categories"] if kind == "category" else snapshot["filters"]
        rows = {item["id"]: item for item in items}
        target = rows.get(ids[0])
        if (not target or target["origin"] != "view" or target["hidden"] is not True
                or not target["editable"]):
            raise ValueError("The override is not a confirmed, editable local hide.")
        warnings = [{"row": ids[0], "blocking": False,
                     "text": "This changes the whole view for current and future members. Review the impact inventory."}]
    else:
        raise ValueError("Choose element, category, or filter.")
    ids = sorted(ids, key=int)
    affected = set(ids if kind == "element" else snapshot["impact"])
    for row in report(expected_after(snapshot, kind, ids))["rows"]:
        if row["id"] not in affected:
            continue
        reasons = ["%s %s (%s)" % (reason["kind"], reason["id"], reason["origin"])
                   if "id" in reason else "a direct element hide" for reason in row["reasons"]]
        if reasons:
            warnings.append({"row": row["id"], "blocking": False,
                             "text": "Other configured hides remain for %s." % ", ".join(reasons)})
    return {"kind": "change-preview", "recipe": "hidden-elements", "title": "Clear Revit visibility hides",
            "keyColumn": "ID", "columns": ["ID", "Source", "Hide flag"], "changedColumns": ["Hide flag"],
            "before": [[key, kind, True] for key in ids], "after": [[key, kind, False] for key in ids],
            "notes": ["Clearing a hide flag does not prove the element is rendered."],
            "warnings": warnings, "blocked": False, "ownership_ids": [snapshot["view"]["id"]],
            "expected_dependent_categories": [], "steps": [{"kind": kind, "id": key} for key in ids],
            "basis": snapshot, "intent": {"kind": kind, "ids": ids,
                                          "allow_whole_view": intent.get("allow_whole_view") is True}}


def check_after(plan, actual):
    intent = plan["intent"]
    expected = expected_after(plan["basis"], intent["kind"], intent["ids"])
    observed = copy.deepcopy(actual)
    for state in (expected, observed):
        state["view"].pop("ownership", None)
        state["view"].pop("writable", None)
    if expected != observed:
        raise ValueError("The target or a reviewed control differs from the planned visibility state.")


def side_effects(plan, data):
    check_after(plan, read_basis(data))
    change = data.get("changes") or {}
    modified = change.get("modified", [])
    view_id = plan["basis"]["view"]["id"]
    allowed = {view_id}
    if plan["intent"]["kind"] == "element":
        allowed.update(plan["intent"]["ids"])
    changed = {str(row["id"]) for row in modified}
    categories = change.get("by_category")
    if (change.get("added_count") != 0 or change.get("deleted_count") != 0 or change.get("other_documents")
            or not changed or not changed <= allowed or len(changed) != len(modified)
            or len(modified) != change.get("modified_count") or not isinstance(categories, dict)
            or sum(categories.values()) != len(modified)):
        raise ValueError("The dry-run change set is incomplete or contains unreviewed changes.")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
    import side_effects as summary
    shown = {"steps": modified}
    print(summary.text_table(summary.summarize(
        shown, {"modified_count": len(modified), "modified_by_category": categories}, list(categories))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("report", "compute", "baseline", "compare", "side-effects"))
    parser.add_argument("first")
    parser.add_argument("second", nargs="?")
    parser.add_argument("output", nargs="?")
    args = parser.parse_args()
    try:
        first = load(args.first)
        if args.command == "report":
            print(json.dumps(report(read_basis(first)), indent=2))
        elif args.command == "compute":
            plan = compute(read_basis(first), load(args.second))
            with io.open(args.output, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            second = load(args.second)
            if args.command == "baseline":
                if first["basis"] != read_basis(second):
                    raise ValueError("The original visibility state changed.")
                print("The original visibility state matches the preview baseline.")
            elif args.command == "compare":
                check_after(first, read_basis(second))
                print("The reviewed visibility flags match the preview.")
            else:
                side_effects(first, second)
    except (ValueError, KeyError, AttributeError, TypeError, IOError) as error:
        print("Visibility review stopped: %s" % error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
