# -*- coding: utf-8 -*-
"""Preview distinct view filter colors and matching drafting legend rows."""
from __future__ import print_function

import argparse
from collections import Counter
from colorsys import hsv_to_rgb
import json
import re
import sys

from view_duplicate_preview import TEXT, element_id, read_payload, read_record, snapshot_digest
from view_filter_transfer_preview import MAX_SAVED_RECORD, checked_changes, checked_record, complete_record

COLOR_FIELDS = ("ProjectionLineColor", "CutLineColor", "SurfaceForegroundPatternColor", "CutForegroundPatternColor")
COLUMNS = ["Row", "Value", "Elements", "Filter", "Action", "Color"]
MAX_VALUES = 24
POSITION_TOLERANCE = 1e-7
COLOR_TRIAL_TRANSACTION = "Test parameter color drafting legend"


def color_text(rgb):
    return "#%02X%02X%02X" % tuple(rgb)


def applied_color(candidate):
    if not candidate or not candidate["applied"]:
        return None
    settings = candidate["graphics"]
    colors = [settings.get(field) for field in COLOR_FIELDS]
    first = colors[0]
    if (not isinstance(first, list) or len(first) != 3
            or any(type(channel) is not int or not 0 <= channel <= 255 for channel in first)
            or any(item != first for item in colors)
            or not settings.get("IsSurfaceForegroundPatternVisible")
            or not settings.get("IsCutForegroundPatternVisible")):
        return None
    return tuple(first)


def assign_colors(groups, chosen):
    colors, conflicts, used = {}, [], set()
    for group in groups:
        group_id = group["key"]
        old = applied_color(chosen[group_id])
        if old is None:
            continue
        if old in used:
            conflicts.append(group_id)
            continue
        used.add(old)
        colors[group_id] = old
    # Reserve consistent applied colors first so new values do not shift them.
    palette = [tuple(int(round(channel * 255)) for channel in hsv_to_rgb(slot / 32.0, 0.73, 0.84))
               for slot in range(32)]
    for group in groups:
        group_id = group["key"]
        if group_id in colors:
            continue
        start = int(group_id[:8], 16) % len(palette)
        choice = next(palette[(start + offset) % len(palette)] for offset in range(len(palette))
                      if palette[(start + offset) % len(palette)] not in used)
        colors[group_id] = choice
        used.add(choice)
    return colors, conflicts


def legend_label(group):
    if not group["has_value"]:
        return "(unset)"
    if group["value"] == "":
        return "(empty string)"
    if not group["labels"]:
        return str(group["value"])
    label = group["labels"][0]
    if len(group["labels"]) != 1 or any(ord(character) < 32 or ord(character) == 127 for character in label):
        return json.dumps(group["value"], ensure_ascii=True)
    return label


def workflow_identity(inventory):
    return snapshot_digest({"document": inventory["document"], "view_uid": inventory["view"]["uid"],
                            "parameter_id": inventory["parameter_id"], "categories": inventory["category_ids"]})


def legend_name(workflow):
    return "Parameter colors - " + workflow[:12]


def proposed_filter_name(workflow, group_id, label):
    readable = re.sub(r"[^A-Za-z0-9]+", " ", label).strip()[:28].strip() or "Value"
    fingerprint = snapshot_digest([workflow, group_id])
    return "Parameter color - %s - %s" % (readable, fingerprint[:12])


def legend_layout(rows, resources):
    size = resources["text"]["text_size_ft"]
    if type(size) not in (int, float) or not 0 < size < 1:
        raise ValueError("The selected legend text size is outside the drafting layout bounds.")
    result = []
    for row in rows:
        top = -4 * size * row["slot"]
        result.append(dict(row, swatch_bounds_ft=[0, top - 1.5 * size, 5 * size, top],
                           label_origin_ft=[7 * size, top]))
    return result


def close_points(actual, expected):
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(close_points(a, b) for a, b in zip(actual, expected)))
    return isinstance(actual, (int, float)) and abs(actual - expected) <= POSITION_TOLERANCE


def note_fits_row(bounds, row, text_size):
    return (bounds is not None and bounds[0] > row["swatch_bounds_ft"][2]
            and bounds[1] > row["label_origin_ft"][1] - 4 * text_size
            and bounds[3] <= row["label_origin_ft"][1] + POSITION_TOLERANCE)


def frame_point(frame, x, y):
    return [base + x * right + y * up for base, right, up in
            zip(frame["origin"], frame["right"], frame["up"])]


def existing_legend_matches(preflight, rows):
    existing, resources = preflight["existing"], preflight["resources"]
    if not existing.get("frame") or existing["scale"] != 1 or len(existing["contents"]) != 2 * len(rows):
        return None
    contents = {item["id"]: item for item in existing["contents"]}
    if (len(contents) != len(existing["contents"])
            or len({item["uid"] for item in contents.values()}) != len(contents)
            or any(item["owner_id"] != existing["id"] or not item.get("visible") for item in contents.values())):
        return None
    solid = {"kind": "element", "id": resources["solid"]["id"]}
    frame = existing["frame"]
    pairs = []
    for row in rows:
        left, bottom, right, top = row["swatch_bounds_ft"]
        corners = [frame_point(frame, x, y) for x, y in ((left, bottom), (right, bottom), (right, top), (left, top))]
        edges = [sorted(sorted([a, b]) for a, b in zip(corners, corners[1:] + corners[:1]))]
        region_matches, note_matches = [], []
        for item in contents.values():
            if item["kind"] == "region" and item["type_id"] == resources["region"]["id"]:
                settings = item["graphics"]
                if (close_points(item["lines"], edges) and settings["SurfaceForegroundPatternId"] == solid
                        and settings["SurfaceForegroundPatternColor"] == row["rgb"]
                        and settings["IsSurfaceForegroundPatternVisible"] is True
                        and settings["IsSurfaceBackgroundPatternVisible"] is False):
                    region_matches.append(item)
            elif (item["kind"] == "note" and item["type_id"] == resources["text"]["id"]
                    and item["text"] == row["label"] and item["plain"] and item["leaders"] == 0
                    and close_points(item["position"], frame_point(frame, *row["label_origin_ft"]))
                    and note_fits_row(item["bounds_ft"], row, resources["text"]["text_size_ft"])):
                note_matches.append(item)
        if len(region_matches) != 1 or len(note_matches) != 1:
            return None
        region, note = region_matches[0], note_matches[0]
        del contents[region["id"]]
        del contents[note["id"]]
        pairs.append({"value_key": row["value_key"], "region_id": region["id"], "region_uid": region["uid"],
                      "note_id": note["id"], "note_uid": note["uid"]})
    return {"view_id": existing["id"], "view_uid": existing["uid"], "pairs": pairs,
            "placements": existing["placements"]}


def normalized_rule(expression):
    # A non-inverted one-child logical node does not change the predicate.
    while (expression and expression["type"] in ("LogicalAndFilter", "LogicalOrFilter")
           and not expression["inverted"] and len(expression["children"]) == 1):
        expression = expression["children"][0]
    return expression


def matching_applied_filter(row, candidate, solid_id):
    if (not candidate or not candidate["applied"] or not candidate["visible"] or not candidate["enabled"]
            or applied_color(candidate) != tuple(row["rgb"])):
        return False
    solid = {"kind": "element", "id": solid_id}
    return all(candidate["graphics"].get(side + "ForegroundPatternId") == solid for side in ("Surface", "Cut"))


def build(inventory):
    rules = inventory["native_rules"]
    if inventory["kind"] != "view-color-parameter-inventory" or rules["status"] != "verified_host_partition":
        raise ValueError("A verified host rule partition is required for a color preview.")
    basis = snapshot_digest(inventory)
    groups = sorted(rules["groups"], key=lambda group: group["key"])
    group_ids = {group["key"] for group in groups}
    if (not 1 <= len(groups) <= MAX_VALUES or len(group_ids) != len(groups)
            or any(not re.match(r"^[0-9a-f]{64}\Z", group_id) for group_id in group_ids)):
        raise ValueError("The value groups are incomplete, repeated or outside the supported scope.")
    members = [element_id(member) for group in groups for member in group["member_ids"]]
    if (Counter(members) != Counter(element_id(row["id"]) for row in inventory["rows"])
            or len(set(members)) != len(members)
            or any(group["count"] != len(group["member_ids"]) or not group["member_ids"] for group in groups)):
        raise ValueError("The value groups do not account for every observed element exactly once.")

    reuse = inventory["existing_filters"]["groups"]
    if set(reuse) != group_ids:
        raise ValueError("The filter reuse inventory does not cover every value group.")
    chosen = {}
    for group_id, match in reuse.items():
        selected = match["selected_id"]
        candidates = [item for item in match["exact"] if item["id"] == selected]
        if match["status"] == "reuse" and len(candidates) != 1:
            raise ValueError("The selected existing filter is absent or ambiguous.")
        if match["status"] not in ("reuse", "create", "blocked") or (match["status"] != "reuse" and selected):
            raise ValueError("The filter reuse inventory contains an invalid decision.")
        chosen[group_id] = candidates[0] if candidates else None

    colors, conflicts = assign_colors(groups, chosen)
    workflow = workflow_identity(inventory)
    names = {row["name"].upper() for row in inventory["existing_filters"]["existing_parameter_filter_names"]}
    labels = {group["key"]: legend_label(group) for group in groups}
    counts = Counter(labels.values())
    for group in groups:
        group_id = group["key"]
        if counts[labels[group_id]] > 1:
            labels[group_id] += " [%s %s]" % (group["storage"], json.dumps(group["value"], ensure_ascii=True))
    counts = Counter(labels.values())
    warnings, before, after, legend = [], [], [], []
    if inventory["link_candidate_ids"]:
        warnings.append({"row": groups[0]["key"], "blocking": True,
                         "text": "Linked elements may be visible. This inventory covers host elements only."})
    for group in groups:
        group_id = group["key"]
        label = labels[group_id]
        if counts[label] > 1:
            label += " [" + group_id[:12] + "]"
        match = reuse[group_id]
        selected = chosen[group_id]
        action = {"reuse": "Reuse filter", "create": "Create filter", "blocked": "Unresolved match"}[match["status"]]
        filter_name = selected["name"] if selected else proposed_filter_name(workflow, group_id, label)
        if match["status"] == "blocked":
            warnings.append({"row": group_id, "text": "Resolve the existing filter match before applying colors.",
                             "blocking": True})
        if match["status"] == "create":
            if filter_name.upper() in names:
                warnings.append({"row": group_id, "text": "The proposed filter name is already in use.",
                                 "blocking": True})
            names.add(filter_name.upper())
        if group_id in conflicts:
            warnings.append({"row": group_id, "text": "Two existing value filters use the same color.",
                             "blocking": True})
        old = applied_color(selected)
        rgb = colors[group_id]
        before.append([group_id, label, group["count"], selected["name"] if selected else None, None,
                       color_text(old) if old is not None else None])
        after.append([group_id, label, group["count"], filter_name, action, color_text(rgb)])
        legend.append({"value_key": group_id, "label": label, "rgb": list(rgb), "filter_name": filter_name,
                       "filter_id": selected["id"] if selected else None, "slot": len(legend)})
    native_legend = inventory.get("legend_preflight")
    legend_reuse = None
    if native_legend is not None:
        if native_legend["name"] != legend_name(workflow):
            raise ValueError("The native legend preflight does not match the proposed view name.")
        legend = legend_layout(legend, native_legend["resources"])
        if native_legend["state"] == "existing_requires_verification":
            legend_reuse = existing_legend_matches(native_legend, legend)
        if native_legend["state"] != "available" and legend_reuse is None:
            warnings.append({"row": groups[0]["key"], "blocking": True,
                             "text": "Resolve the native legend preflight state before using this proposal: "
                                     + native_legend["state"] + "."})
    no_change_candidate = bool(legend_reuse) and not warnings and all(
        matching_applied_filter(row, chosen[row["value_key"]], native_legend["resources"]["solid"]["id"])
        for row in legend)
    if no_change_candidate:
        for row in after:
            row[4] = "Filter and legend match"
    proposal = {"kind": "view-color-proposal", "basis_sha256": basis, "workflow_sha256": workflow,
                "document": inventory["document"], "view": inventory["view"],
                "legend_name": legend_name(workflow), "legend_rows": legend, "legend_preflight": native_legend,
                "legend_reuse": legend_reuse, "no_change_candidate": no_change_candidate,
                "blocked": bool(warnings)}
    display = {"kind": "change-preview", "recipe": "view-color", "title": "Color view by parameter",
               "keyColumn": "Row", "columns": COLUMNS, "changedColumns": COLUMNS[3:],
               "before": before, "after": after, "warnings": warnings, "review_only": True,
               "basis_sha256": basis, "blocked": bool(warnings), "steps": [], "ownership_ids": []}
    snapshot_digest(proposal)
    snapshot_digest(display)
    return proposal, display


def trial_changes(baseline, proposal, receipt, combined):
    rows = proposal["legend_rows"]
    observed = receipt["rows"]
    if [row["value_key"] for row in observed] != [row["value_key"] for row in rows]:
        raise ValueError("The trial did not verify every reviewed legend row.")
    identities = [(receipt["legend_id"], receipt["legend_uid"])]
    for row, measured in zip(rows, observed):
        if measured["rgb"] != row["rgb"]:
            raise ValueError("A trial swatch differs from its reviewed color.")
        identities.extend((measured[role + "_id"], measured[role + "_uid"]) for role in ("region", "note"))
    added = [element_id(item) for item, _ in identities]
    modified = []
    if combined:
        filters = receipt["filters"]
        if [item["value_key"] for item in filters] != [row["value_key"] for row in rows]:
            raise ValueError("The trial did not verify every reviewed filter.")
        rules = {group["key"]: group["expression"] for group in baseline["native_rules"]["groups"]}
        groups = baseline["existing_filters"]["groups"]
        solid = proposal["legend_preflight"]["resources"]["solid"]["id"]
        for row, item in zip(rows, filters):
            value = row["value_key"]
            created = row["filter_id"] is None
            current = next((candidate for candidate in groups[value]["exact"]
                            if candidate["id"] == row["filter_id"]), None)
            if (item["created"] is not created or item["name"] != row["filter_name"]
                    or item["expression"] != normalized_rule(rules[value])
                    or (not created and (item["filter_id"] != row["filter_id"]
                                         or item["filter_uid"] != current["uid"]))
                    or not matching_applied_filter(
                        row, dict(item, applied=True, visible=True, enabled=True), solid)):
                raise ValueError("A trial filter differs from its reviewed identity, rule or graphics.")
            identities.append((item["filter_id"], item["filter_uid"]))
            if created:
                added.append(element_id(item["filter_id"]))
            if not matching_applied_filter(row, current, solid):
                modified = [proposal["view"]["id"]]
    elif "filters" in receipt:
        raise ValueError("A legend-only trial reported filter changes.")

    ids = [element_id(item) for item, _ in identities]
    uids = [uid for _, uid in identities]
    existing_ids = {proposal["view"]["id"]} | {row["id"] for row in baseline["rows"]}
    existing_ids.update(row["id"] for row in baseline["existing_filters"]["existing_parameter_filter_names"])
    existing_ids.update(resource["id"] for resource in proposal["legend_preflight"]["resources"].values())
    if (len(set(ids)) != len(ids) or len(set(uids)) != len(uids) or set(added) & existing_ids
            or any(not isinstance(uid, TEXT) or not uid for uid in uids)):
        raise ValueError("The trial has repeated, missing or already existing identities.")
    changes = {"added": sorted(added, key=int), "modified": modified, "deleted": []}
    if receipt["changes"] != changes:
        raise ValueError("The trial change list disagrees with the reviewed additions and model view.")
    return changes


def verify_fixture(review, original_record, probe_record, fresh_record):
    original = checked_record(original_record, "query")
    probe = checked_record(probe_record, "dry_run")
    fresh = checked_record(fresh_record, "query")
    if len({record["run_id"] for record in (original, probe, fresh)}) != 3:
        raise ValueError("Supply three distinct pyRevit run records.")
    checked_changes(original, [])
    checked_changes(fresh, [])
    baseline = read_payload(original, "inventory_json")
    proposal, preview = build(baseline)
    if review != {"proposal": proposal, "preview": preview} or proposal["blocked"] or not proposal["legend_preflight"]:
        raise ValueError("The original query does not reproduce the reviewed color proposal and preview.")
    if read_payload(fresh, "inventory_json") != baseline:
        raise ValueError("The independent Revit query differs from the original color inventory.")
    result = probe["result"]
    receipt = result["receipt"]
    if receipt["basis_sha256"] != proposal["basis_sha256"]:
        raise ValueError("The probe receipt does not match the reviewed color inventory.")
    status = result["status"]
    if status == "color_fixture_unchanged" and proposal["no_change_candidate"]:
        filter_ids = [row["filter_id"] for row in proposal["legend_rows"]]
        order = receipt["ordered_filter_ids"]
        if (receipt["legend_reuse"] != proposal["legend_reuse"]
                or order != baseline["existing_filters"]["ordered_applied_ids"]
                or Counter(order) != Counter(filter_ids)):
            raise ValueError("The repeat receipt differs from the reviewed filter or legend identities.")
        changes = {"added": [], "modified": [], "deleted": []}
        verified = "verified_unchanged_color_fixture"
    elif (status in ("color_trial_rolled_back", "legend_trial_rolled_back")
          and proposal["legend_preflight"]["state"] == "available"):
        changes = trial_changes(baseline, proposal, receipt, status == "color_trial_rolled_back")
        verified = "verified_color_fixture_rollback" if status == "color_trial_rolled_back" else "verified_legend_rollback"
    else:
        raise ValueError("The probe outcome does not match the reviewed fixture.")
    checked_changes(probe, changes["modified"], changes["added"], COLOR_TRIAL_TRANSACTION)
    return {"status": verified, "basis_sha256": proposal["basis_sha256"], "document": proposal["document"],
            "original_run_id": original["run_id"], "probe_run_id": probe["run_id"],
            "fresh_run_id": fresh["run_id"], "changes": changes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_record", help="Full pyRevit color and reuse inventory")
    parser.add_argument("--verify-fixture", nargs=3, metavar=("REVIEW", "PROBE", "FRESH"),
                        help="Verify the saved review, full dry-run response, and independent fresh query")
    args = parser.parse_args()
    try:
        record = read_record(args.runtime_record, MAX_SAVED_RECORD)
        if args.verify_fixture:
            review, probe, fresh = [read_record(path, MAX_SAVED_RECORD) for path in args.verify_fixture]
            report = verify_fixture(review, record, probe, fresh)
            print(json.dumps(report, ensure_ascii=True, indent=2))
            return 0
        inventory = read_payload(complete_record(record), "inventory_json")
        proposal, preview = build(inventory)
        print(json.dumps({"proposal": proposal, "preview": preview}, ensure_ascii=True, indent=2))
        return 1 if proposal["blocked"] else 0
    except (ValueError, KeyError, TypeError, AttributeError, IOError) as error:
        print("View color preview stopped: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
