# -*- coding: utf-8 -*-
"""Compute additions, label changes and retirements for a door and window legend."""
from __future__ import print_function

import argparse
import io
from itertools import product
import json
import re
import sys

LIMIT = 2 * 1024 * 1024
MAX_ROWS = 100
EPSILON = 1e-7
TEXT = (str, type(u""))
LABEL_LIMIT = 240
ID = re.compile(r"^[1-9][0-9]{0,18}\Z")


def valid_id(value):
    return isinstance(value, TEXT) and ID.match(value) and int(value) < 2**63


def index(rows, field):
    indexed = {}
    for row in rows:
        value = row[field]
        if not isinstance(value, TEXT) or not value or value in indexed:
            raise ValueError("The inventory contains missing or repeated identities.")
        indexed[value] = row
    return indexed


def safe_label(value):
    return (isinstance(value, TEXT) and 1 <= len(value) <= LABEL_LIMIT and value.strip() == value
            and not any(ord(character) < 32 or ord(character) == 127 for character in value))


def number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high:
        raise ValueError("A legend layout value is outside its range.")
    return value


def vector(values, size):
    if not isinstance(values, list) or len(values) != size:
        raise ValueError("A legend layout vector has the wrong size.")
    return [number(value, -1e6, 1e6) for value in values]


def dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def view_rectangle(bounds, legend):
    low, high = vector(bounds["min"], 3), vector(bounds["max"], 3)
    if any(a > b for a, b in zip(low, high)):
        raise ValueError("A legend graphic has invalid bounds.")
    origin, right, up = [vector(legend[field], 3) for field in ("origin_ft", "right", "up")]
    if any(abs(value) > EPSILON for value in (dot(right, up), dot(right, right) - 1, dot(up, up) - 1)):
        raise ValueError("The legend view frame is not orthonormal.")
    corners = [[value - offset for value, offset in zip(corner, origin)]
               for corner in product(*zip(low, high))]
    projected = [[dot(corner, right), dot(corner, up)] for corner in corners]
    return [min(row[0] for row in projected), min(row[1] for row in projected),
            max(row[0] for row in projected), max(row[1] for row in projected)]


def cell_bounds(grid, slot):
    row, column = divmod(slot, grid["columns"])
    u, v = grid["origin_uv_ft"]
    width, height = grid["cell_width_ft"], grid["cell_height_ft"]
    left, top = u + column * width, v - row * height
    return [left, top - height, left + width, top]


def overlaps(a, b, clearance):
    return (a[0] < b[2] + clearance and b[0] < a[2] + clearance
            and a[1] < b[3] + clearance and b[1] < a[3] + clearance)


def fits(inner, outer, clearance):
    return (inner[0] >= outer[0] + clearance - EPSILON and inner[1] >= outer[1] + clearance - EPSILON
            and inner[2] <= outer[2] - clearance + EPSILON and inner[3] <= outer[3] - clearance + EPSILON)


def display_dimension(row, field):
    if row["instance_dimensions_vary"][field]:
        return None
    nominal = row[field]
    if nominal:
        return nominal["display"] or u"%.4g ft" % nominal["feet"]
    bounds = row["instance_dimension_ranges_ft"][field]
    if bounds and row["instance_dimension_counts"][field] == row["placements"] and abs(bounds[1] - bounds[0]) <= EPSILON:
        return u"%.4g ft" % bounds[0]
    return None


def label(row, overrides):
    if row["uid"] in overrides:
        return overrides[row["uid"]]
    width, height = (display_dimension(row, field) for field in ("width", "height"))
    mark = row["type_mark"]
    if not mark or not width or not height:
        return None
    proposed = u"%s | %s × %s" % (mark, width, height)
    return proposed if safe_label(proposed) else None


def paired_state(row, graphic, kind):
    common = {"id": row["id"], "uid": row["uid"], "owner_id": graphic["owner_id"],
              "pinned": graphic["pinned"], "group_id": graphic["group_id"], "category_id": graphic["category_id"],
              "native_kind": graphic["native_kind"]}
    if graphic["uid"] != row["uid"] or not valid_id(row["id"]):
        raise ValueError("The legend row no longer matches its graphic.")
    if kind == "component":
        fields = ("type_uid", "type_id", "direction_raw", "host_length_ft", "position_ft")
        if row["position_ft"] is None:
            fields += ("bounds_ft",)
    else:
        appearance = graphic.get("text_appearance")
        if not appearance or not appearance["plain_single_line"] or appearance["leaders"] != 0:
            raise ValueError("A managed note has unverified formatting or leaders.")
        common["text_appearance"] = appearance
        fields = ("text", "type_id", "position_ft")
    common.update((key, row[key]) for key in fields)
    return common


def managed_pairs(snapshot, legend, intent, components, notes, graphics):
    receipt = intent.get("receipt")
    if receipt is None:
        if any(row["native_kind"] != "ExtentElem" for row in graphics.values()):
            raise ValueError("Use an empty dedicated target legend or its verified prior receipt.")
        return {}
    if (receipt["kind"] != "opening-legend-receipt" or receipt["decision"] != "committed"
            or receipt["approval"] not in ("user", "auto")
            or not receipt["run_id"] or not receipt["readback_run_id"] or receipt["run_id"] == receipt["readback_run_id"]
            or receipt["host_identity"] != snapshot["host_identity"] or receipt["legend_uid"] != legend["uid"]
            or receipt["grid"] != intent["grid"]):
        raise ValueError("The prior receipt does not match this project, legend, and grid.")
    pairs = index(receipt["pairs"], "type_uid")
    used_ids, slots = set(), set()
    for uid, pair in pairs.items():
        slot = pair["slot"]
        if type(slot) is not int or not 0 <= slot < MAX_ROWS or slot in slots:
            raise ValueError("The prior receipt has an invalid or repeated grid slot.")
        slots.add(slot)
        for kind, rows in (("component", components), ("note", notes)):
            expected = pair[kind]
            identifier = expected["id"]
            current = rows.get(identifier)
            if (identifier in used_ids or current is None or identifier not in graphics
                    or paired_state(current, graphics[identifier], kind) != expected or expected["owner_id"] != legend["id"]):
                raise ValueError("A managed component or label changed after its kept receipt.")
            used_ids.add(identifier)
        component = components[pair["component"]["id"]]
        if component["type_uid"] != uid or component["direction"] != "front_elevation":
            raise ValueError("A managed component no longer represents the reviewed front elevation.")
    return pairs


def rectangles(legend, intent):
    sheets = {row["id"]: row for row in legend["layout"]["sheet_placements"]}
    proposed = intent.get("sheet_rectangles_ft", {})
    if not isinstance(proposed, dict) or set(proposed) != set(sheets):
        raise ValueError("Review one available paper-space rectangle for every sheet showing the legend.")
    for identifier, value in proposed.items():
        rect = vector(value, 4)
        outline = sheets[identifier]["outline_ft"]
        boundary = vector(outline["min"], 2) + vector(outline["max"], 2)
        if rect[0] >= rect[2] or rect[1] >= rect[3] or not fits(rect, boundary, 0):
            raise ValueError("A reviewed sheet rectangle falls outside the sheet outline.")
    return proposed


def build_preview(snapshot, intent):
    if (snapshot["kind"] != "placed-opening-type-snapshot" or snapshot["scope"] != "host-project"
            or set(intent) - {"grid", "labels", "receipt", "sheet_rectangles_ft"}):
        raise ValueError("Use a complete host opening snapshot and a reviewed layout.")
    legend = snapshot["legend"]
    layout = legend["layout"]
    if not legend["directions_verified_for_year"] or not valid_id(legend["id"]):
        raise ValueError("Use a legend with verified Revit 2026 direction values.")
    rectangles(legend, intent)
    grid = intent["grid"]
    if (set(grid) != {"origin_uv_ft", "columns", "cell_width_ft", "cell_height_ft"}
            or type(grid["columns"]) is not int or not 1 <= grid["columns"] <= 20):
        raise ValueError("Supply the origin, column count and cell dimensions.")
    vector(grid["origin_uv_ft"], 2)
    for key in ("cell_width_ft", "cell_height_ft"):
        number(grid[key], 0.25, 100)
    clearance = number(legend["scale"], 1, 1000) / 96.0
    if min(grid["cell_width_ft"], grid["cell_height_ft"]) <= 2 * clearance:
        raise ValueError("The grid does not have room for the paper clearance.")
    types = index(snapshot["types"], "uid")
    if len(types) > MAX_ROWS or any(not valid_id(row["id"]) for row in types.values()):
        raise ValueError("Review up to 100 placed door and window types.")
    overrides = intent.get("labels", {})
    if not isinstance(overrides, dict) or any(uid not in types or not safe_label(value) for uid, value in overrides.items()):
        raise ValueError("Supply single-line labels only for reviewed type UIDs.")
    components, notes = index(legend["components"], "id"), index(legend["text_notes"], "id")
    graphics = index(layout["target_graphics"], "id")
    pairs = managed_pairs(snapshot, legend, intent, components, notes, graphics)
    paired_ids = {pair[kind]["id"] for pair in pairs.values() for kind in ("component", "note")}
    for component in components.values():
        if component["id"] not in paired_ids and component["opening_category"]:
            raise ValueError("Review the unproven door or window component before updating this legend.")
    manual = [row for key, row in graphics.items() if key not in paired_ids and row["native_kind"] != "ExtentElem"]
    if any(row["bounds_ft"] is None for row in manual):
        raise ValueError("A manual legend graphic has unknown bounds.")
    manual_rects = [view_rectangle(row["bounds_ft"], legend) for row in manual]
    return build_rows(snapshot, intent, legend, types, pairs, graphics, manual_rects, clearance)


def build_rows(snapshot, intent, legend, types, pairs, graphics, manual_rects, clearance):
    grid, overrides = intent["grid"], intent.get("labels", {})
    occupied = {pair["slot"] for uid, pair in pairs.items() if uid in types}
    slots = [slot for slot in range(MAX_ROWS) if slot not in occupied]
    warnings, steps, before, after, ownership = [], [], [], [], set()
    marks = {}
    for row in types.values():
        if row["type_mark"]:
            marks[row["type_mark"]] = marks.get(row["type_mark"], 0) + 1
    keys = sorted(set(types) | set(pairs), key=lambda uid: (uid not in types, types[uid]["category"] if uid in types else "", uid))
    for uid in keys:
        row, prior = types.get(uid), pairs.get(uid)
        if prior is not None:
            existing_ids = [prior["component"]["id"], prior["note"]["id"]]
            old = [uid, prior["component"]["type_id"], prior["note"]["text"], prior["slot"] + 1, ", ".join(existing_ids)]
        else:
            existing_ids, old = [], [uid, None, None, None, None]
        if row is None:
            operation, slot, proposed = "retire", prior["slot"], None
            new = [uid, None, None, None, None]
        else:
            if prior:
                slot = prior["slot"]
            else:
                slot = next((candidate for candidate in slots if not any(
                    overlaps(cell_bounds(grid, candidate), other, clearance) for other in manual_rects
                )), slots[0])
                slots.remove(slot)
            proposed = label(row, overrides)
            rect = cell_bounds(grid, slot)
            operation = "add" if prior is None else "refresh"
            if (prior is not None and proposed == prior["note"]["text"]
                    and prior["note"]["type_id"] == legend["layout"]["text_type"]["id"]
                    and all(
                    fits(view_rectangle(graphics[key]["bounds_ft"], legend), rect, clearance)
                    for key in existing_ids if graphics[key]["bounds_ft"] is not None
            ) and all(graphics[key]["bounds_ft"] is not None for key in existing_ids)):
                operation = "unchanged"
            if proposed is None:
                warnings.append({"row": uid, "text": "Review a label for missing or variable mark or size.", "blocking": True})
            if row["type_mark"] and marks[row["type_mark"]] > 1:
                warnings.append({"row": uid, "text": "This type mark is shared by multiple placed types.", "blocking": False})
            if any(overlaps(rect, other, clearance) for other in manual_rects):
                warnings.append({"row": uid, "text": "The proposed cell overlaps a manual graphic.", "blocking": True})
            new = [uid, row["id"], proposed, slot + 1, ", ".join(existing_ids) if existing_ids else "New pair"]
        before.append(old + [None])
        after.append(new + [operation if operation != "unchanged" else None])
        if operation != "unchanged":
            if prior and any(graphics[key]["pinned"] or graphics[key]["group_id"] != "-1" for key in existing_ids):
                warnings.append({"row": uid, "text": "A managed element is pinned or grouped.", "blocking": True})
            ownership.update(existing_ids)
            steps.append({"operation": operation, "type_uid": uid, "type_id": row["id"] if row else None,
                          "label": proposed, "slot": slot, "existing_ids": existing_ids,
                          "cell_uv_ft": cell_bounds(grid, slot) if row else None})
    if steps:
        ownership.add(legend["id"])
    # Retire old rows before using their freed cells.
    steps.sort(key=lambda step: (step["operation"] != "retire", step["slot"]))
    fields = ["Type UID", "Type ID", "Label", "Cell", "Legend elements", "Action"]
    return {"kind": "change-preview", "recipe": "opening-elevation-legend", "title": "Update door and window legend",
            "columns": fields, "keyColumn": fields[0], "changedColumns": fields[1:], "before": before, "after": after,
            "steps": steps, "ownership_ids": sorted(ownership, key=int), "warnings": warnings,
            "blocked": any(warning["blocking"] for warning in warnings), "basis": snapshot, "intent": intent,
            "notes": ["Revit must verify the component, note and sheet bounds after regeneration.",
                      "Sheet placements to check: %d." % len(legend["layout"]["sheet_placements"])]}


def paper_rectangle(bounds):
    if bounds is None:
        raise ValueError("A reviewed sheet graphic has unknown bounds.")
    result = vector(bounds["min"], 3)[:2] + vector(bounds["max"], 3)[:2]
    if result[0] > result[2] or result[1] > result[3]:
        raise ValueError("A reviewed sheet graphic has reversed bounds.")
    return result


def verify_sheet_fit(legend_before, legend, intent):
    old = index(legend_before["layout"]["sheet_placements"], "id")
    actual = index(legend["layout"]["sheet_placements"], "id")
    reviewed = rectangles(legend, intent)
    if set(actual) != set(old):
        raise ValueError("The legend's set of sheet placements changed.")
    changed_viewports = []
    for key, sheet in actual.items():
        prior = old[key]
        if any(sheet[field] != prior[field] for field in ("uid", "number", "outline_ft", "legend_viewport_ids")):
            raise ValueError("A reviewed sheet or viewport changed identity.")
        graphics = index(sheet["graphics"], "id")
        before = index(prior["graphics"], "id")
        if set(graphics) != set(before):
            raise ValueError("An unreviewed sheet graphic changed.")
        legend_ids = set(sheet["legend_viewport_ids"])
        others = []
        for identifier, row in graphics.items():
            if identifier in legend_ids:
                fields = set(row) - {"bounds_ft", "label_bounds_ft"}
                if {field: row[field] for field in fields} != {field: before[identifier][field] for field in fields}:
                    raise ValueError("A legend viewport changed a property outside its measured outlines.")
                if row != before[identifier]:
                    changed_viewports.append(identifier)
                continue
            if row != before[identifier]:
                raise ValueError("An adjacent sheet graphic changed.")
            if row["native_kind"] == "ExtentElem" or row["category_id"] == legend["layout"]["title_block_category_id"]:
                continue
            others.append(paper_rectangle(row["bounds_ft"]))
            if row.get("label_bounds_ft") is not None:
                others.append(paper_rectangle(row["label_bounds_ft"]))
        for identifier in legend_ids:
            row = graphics[identifier]
            outlines = [row["bounds_ft"]]
            if row["label_bounds_ft"] is not None:
                outlines.append(row["label_bounds_ft"])
            for bounds in outlines:
                rect = paper_rectangle(bounds)
                clearance = 1.0 / 96.0
                if not fits(rect, reviewed[key], clearance) or any(overlaps(rect, other, clearance) for other in others):
                    raise ValueError("The legend or its label does not fit on every reviewed sheet.")
    return sorted(changed_viewports, key=int)


def verify_result(plan, result, pair_ids):
    basis = plan["basis"]
    before, legend = basis["legend"], result["legend"]
    if result["host_identity"] != basis["host_identity"] or result["types"] != basis["types"]:
        raise ValueError("The host identity or placed type census changed during the legend update.")
    for key in ("id", "uid", "scale", "origin_ft", "right", "up", "revit_year"):
        if legend[key] != before[key]:
            raise ValueError("The target legend changed during the update.")
    for key in ("source_legend_uid", "source_legend_id", "source_scale", "source_frame", "seed", "text_type"):
        if legend["layout"][key] != before["layout"][key]:
            raise ValueError("The source seed or reviewed text type changed.")
    coverage = legend["layout"]["front_elevation_coverage"]
    if any(coverage.values()):
        raise ValueError("The resulting legend does not have one front elevation per placed type.")
    types = index(result["types"], "uid")
    if set(pair_ids) != set(types):
        raise ValueError("The legend result did not account for every placed type.")
    components, notes = index(legend["components"], "id"), index(legend["text_notes"], "id")
    graphics = index(legend["layout"]["target_graphics"], "id")
    old_components, old_notes = index(before["components"], "id"), index(before["text_notes"], "id")
    old_graphics = index(before["layout"]["target_graphics"], "id")
    prior = (plan["intent"].get("receipt") or {}).get("pairs", [])
    prior_pairs = index(prior, "type_uid")
    plan_steps = index(plan["steps"], "type_uid")
    old_managed = {pair[kind]["id"] for pair in prior for kind in ("component", "note")}
    final_ids = set()
    pairs = []
    for uid, identities in pair_ids.items():
        component_id, note_id = identities["component_id"], identities["note_id"]
        if component_id == note_id or {component_id, note_id} & final_ids:
            raise ValueError("The result contains a repeated legend pair element.")
        final_ids.update((component_id, note_id))
        component, note = components[component_id], notes[note_id]
        expected_type = types[uid]
        step = plan_steps.get(uid)
        old = prior_pairs.get(uid)
        if old and (component_id != old["component"]["id"] or note_id != old["note"]["id"]
                    or component["uid"] != old["component"]["uid"] or note["uid"] != old["note"]["uid"]):
            raise ValueError("A refresh replaced a reviewed element.")
        if step is None and (component != old_components[component_id] or note != old_notes[note_id]
                             or any(graphics[key] != old_graphics[key] for key in (component_id, note_id))):
            raise ValueError("A legend pair marked unchanged was modified.")
        expected_label = step["label"] if step else old["note"]["text"]
        slot = step["slot"] if step else old["slot"]
        if (component["type_uid"] != uid or component["type_id"] != expected_type["id"]
                or component["direction"] != "front_elevation" or note["text"] != expected_label
                or note["type_id"] != legend["layout"]["text_type"]["id"]):
            raise ValueError("A generated elevation or label differs from the preview.")
        cell = cell_bounds(plan["intent"]["grid"], slot)
        clearance = legend["scale"] / 96.0
        component_rect = view_rectangle(graphics[component_id]["bounds_ft"], legend)
        note_rect = view_rectangle(graphics[note_id]["bounds_ft"], legend)
        if (not fits(component_rect, cell, clearance) or not fits(note_rect, cell, clearance)
                or overlaps(component_rect, note_rect, clearance - EPSILON)
                or not 0 < component_rect[2] - component_rect[0] <= 50
                or not 0 < component_rect[3] - component_rect[1] <= 50):
            raise ValueError("A measured elevation or label does not fit the reviewed cell.")
        for identifier in (component_id, note_id):
            if graphics[identifier]["owner_id"] != legend["id"]:
                raise ValueError("A result element is outside the target legend.")
        pairs.append({"type_uid": uid, "slot": slot, "component": paired_state(component, graphics[component_id], "component"),
                      "note": paired_state(note, graphics[note_id], "note")})
    for before_rows, after_rows in ((before["layout"]["target_graphics"], graphics.values()),
                                    (before["components"], components.values()), (before["text_notes"], notes.values())):
        unchanged_before = {row["id"]: row for row in before_rows
                            if row["id"] not in old_managed and row.get("native_kind") != "ExtentElem"}
        unchanged_after = {row["id"]: row for row in after_rows
                           if row["id"] not in final_ids and row.get("native_kind") != "ExtentElem"}
        if unchanged_before != unchanged_after:
            raise ValueError("Unmanaged graphics changed during the legend update.")
    viewports = verify_sheet_fit(before, legend, plan["intent"])
    return {"host_identity": result["host_identity"], "legend_uid": legend["uid"], "grid": plan["intent"]["grid"],
            "pairs": sorted(pairs, key=lambda pair: pair["slot"]), "changed_viewport_ids": viewports}


def load(path):
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        source = stream.read(LIMIT + 1)
    if len(source) > LIMIT:
        raise ValueError("The legend preview input exceeds 2 MiB.")
    return json.loads(source)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("read")
    parser.add_argument("intent")
    args = parser.parse_args(argv)
    try:
        response = load(args.read)
        response = response.get("response", response)
        if response.get("status") != "ok":
            raise ValueError("The Revit opening query did not complete.")
        response = response.get("result", response)
        if response.get("status") != "ok":
            raise ValueError("The Revit opening inventory is unavailable.")
        preview = build_preview(json.loads(response["snapshot_json"]), load(args.intent))
        encoded = json.dumps(preview, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(encoded) > LIMIT:
            raise ValueError("The legend preview exceeds 2 MiB.")
        print(encoded)
        return 1 if preview["blocked"] else 0
    except (ValueError, IOError, TypeError, KeyError, AttributeError) as error:
        print("Opening legend preview stopped: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
