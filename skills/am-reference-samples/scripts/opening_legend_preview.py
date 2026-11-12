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


def build_preview(snapshot, intent):
    if (snapshot["kind"] != "placed-opening-type-snapshot" or snapshot["scope"] != "host-project"
            or set(intent) - {"grid", "labels", "receipt"}):
        raise ValueError("Use a complete host opening snapshot and a reviewed layout.")
    legend = snapshot["legend"]
    layout = legend["layout"]
    if not legend["directions_verified_for_year"] or not valid_id(legend["id"]):
        raise ValueError("Use a legend with verified Revit 2026 direction values.")
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
