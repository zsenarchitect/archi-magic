# -*- coding: utf-8 -*-
"""Preview distinct view filter colors and matching drafting legend rows."""
from __future__ import print_function

import argparse
from collections import Counter
from colorsys import hsv_to_rgb
import json
import re
import sys

from view_duplicate_preview import element_id, read_payload, read_record, snapshot_digest
from view_filter_transfer_preview import MAX_SAVED_RECORD, complete_record

COLOR_FIELDS = ("ProjectionLineColor", "CutLineColor", "SurfaceForegroundPatternColor", "CutForegroundPatternColor")
COLUMNS = ["Row", "Value", "Elements", "Filter", "Action", "Color"]
MAX_VALUES = 24


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
    if native_legend is not None:
        if native_legend["name"] != legend_name(workflow):
            raise ValueError("The native legend preflight does not match the proposed view name.")
        if native_legend["state"] != "available":
            warnings.append({"row": groups[0]["key"], "blocking": True,
                             "text": "Resolve the native legend preflight state before using this proposal: "
                                     + native_legend["state"] + "."})
        legend = legend_layout(legend, native_legend["resources"])
    proposal = {"kind": "view-color-proposal", "basis_sha256": basis, "workflow_sha256": workflow,
                "document": inventory["document"], "view": inventory["view"],
                "legend_name": legend_name(workflow), "legend_rows": legend, "legend_preflight": native_legend,
                "blocked": bool(warnings)}
    display = {"kind": "change-preview", "recipe": "view-color", "title": "Color view by parameter",
               "keyColumn": "Row", "columns": COLUMNS, "changedColumns": COLUMNS[3:],
               "before": before, "after": after, "warnings": warnings, "review_only": True,
               "basis_sha256": basis, "blocked": bool(warnings), "steps": [], "ownership_ids": []}
    snapshot_digest(proposal)
    snapshot_digest(display)
    return proposal, display


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime_record", help="Full pyRevit color and reuse inventory")
    args = parser.parse_args()
    try:
        record = complete_record(read_record(args.runtime_record, MAX_SAVED_RECORD))
        inventory = read_payload(record, "inventory_json")
        proposal, preview = build(inventory)
        print(json.dumps({"proposal": proposal, "preview": preview}, ensure_ascii=True, indent=2))
        return 1 if proposal["blocked"] else 0
    except (ValueError, KeyError, TypeError, AttributeError, IOError) as error:
        print("View color preview stopped: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
