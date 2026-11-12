# -*- coding: utf-8 -*-
"""Compute the reviewed view settings for a staged Revit family thumbnail."""
from __future__ import print_function

import argparse
import json
import math
import re
import sys

from focus_view_preview import close_values, load, triple


VIEW_NAME = "Archi Magic Family Preview"
STYLE = "Shading"
GRAY = [238, 238, 238]
BACKGROUND = {"type": "Gradient", "colors": {name: GRAY for name in
              ("SkyColor", "BackgroundColor", "GroundColor")}}
TEXT = (str, type(u""))
IDENTIFIER = re.compile(r"^[1-9][0-9]{0,18}\Z")
SHA = re.compile(r"^[a-f0-9]{64}\Z")


def decimal_id(value):
    return isinstance(value, TEXT) and IDENTIFIER.match(value) is not None and int(value) < 2**63


def camera(extent):
    low, high = triple(extent["min_ft"]), triple(extent["max_ft"])
    lengths = [b - a for a, b in zip(low, high)]
    if not all(0 < length <= 10000 for length in lengths):
        raise ValueError("The family solid bounds are empty or exceed the supported range.")
    target = [(a + b) / 2.0 for a, b in zip(low, high)]
    direction = [1 / math.sqrt(3.0)] * 3
    up = [-1 / math.sqrt(6.0), -1 / math.sqrt(6.0), 2 / math.sqrt(6.0)]
    distance = 2 * math.sqrt(sum(length * length for length in lengths))
    eye = triple([value + distance * axis for value, axis in zip(target, direction)])
    return {"target_ft": target, "eye_ft": eye, "forward": [-axis for axis in direction], "up": up}


def review(snapshot, intent):
    if snapshot["kind"] != "family-preview-snapshot" or set(intent) - {"view_type_id", "reuse_receipt"}:
        raise ValueError("Use a family snapshot and a reviewed 3D view type.")
    identity = snapshot["identity"]
    if (not identity["family_uid"]
            or not all(SHA.match(identity[field]) for field in ("staged_path_sha256", "staged_file_sha256"))):
        raise ValueError("The staged family identity is incomplete.")
    if snapshot["read_only"] is not False or snapshot["document_modified"] is not False:
        raise ValueError("Use a clean, writable staged family.")
    if snapshot["classification"] != "solid_geometry_observed":
        raise ValueError("Read an uncropped orthographic view with observed solid geometry.")
    view_type = intent["view_type_id"]
    if not decimal_id(view_type):
        raise ValueError("Use a decimal-string Revit view type ID.")
    eligible = [row for row in snapshot["three_dimensional_view_types"] if row["id"] == view_type]
    if len(eligible) != 1 or not eligible[0]["uid"]:
        raise ValueError("The reviewed 3D view type is unavailable.")
    extent = snapshot["geometry"]
    ids = extent["element_ids"]
    if not 1 <= len(ids) <= 2000 or not all(decimal_id(value) for value in ids) or len(set(ids)) != len(ids):
        raise ValueError("The solid element inventory is incomplete.")
    desired = {"name": VIEW_NAME, "view_type": eligible[0], "orientation": camera(extent),
               "display_style": STYLE, "background": BACKGROUND, "force_preview_update": True}
    matches = snapshot["named_preview_views"]
    previous = snapshot["preview_view"]
    existing = None
    action = "create"
    receipt = intent.get("reuse_receipt")
    if matches or receipt is not None:
        if (not isinstance(receipt, dict) or set(receipt) != {"view_uid", "family_uid", "saved_file_sha256"}
                or receipt["family_uid"] != identity["family_uid"]
                or receipt["saved_file_sha256"] != identity["staged_file_sha256"]
                or not receipt["view_uid"] or len(matches) != 1 or matches[0]["uid"] != receipt["view_uid"]
                or matches[0]["name"] != VIEW_NAME):
            raise ValueError("Verify the preview view UID from the prior receipt before reusing that name.")
        existing = matches[0]
        if (existing["template"] or existing.get("perspective", True)
                or existing.get("locked", True) or existing.get("section_box", True)
                or existing.get("crop_box", True) or existing.get("view_template_id") != "-1"
                or existing.get("view_type_id") != view_type):
            raise ValueError("The existing view settings prevent safe reuse.")
        action = "reuse"
        saved = existing["saved_orientation"]
        if (saved and previous and previous["uid"] == existing["uid"] and snapshot["force_preview_update"] is True
                and all(close_values(desired["orientation"][key], saved[key]) for key in ("eye_ft", "forward", "up"))
                and existing["display_style"] == STYLE and existing["background"] == BACKGROUND):
            action = "no_change"

    return preview(snapshot, intent, desired, previous, existing, action)


def view_row(view, force):
    value = view or {}
    camera_state = value.get("saved_orientation") or value.get("orientation") or {}
    colors = (value.get("background") or {}).get("colors", {})
    return [value.get("name") or "(active view on save)", value.get("display_style"),
            (value.get("background") or {}).get("type"), colors.get("SkyColor"),
            colors.get("BackgroundColor"), colors.get("GroundColor"),
            camera_state.get("eye_ft"), camera_state.get("forward"), camera_state.get("up"), force]


def preview(snapshot, intent, desired, previous, existing, action):
    key = "Family thumbnail"
    fields = ["Preview view", "Display style", "Background", "Sky RGB", "Horizon RGB", "Ground RGB", "Eye (ft)", "Forward", "Up",
              "Regenerate preview"]
    before = [key] + view_row(previous, snapshot["force_preview_update"])
    after = [key] + view_row(desired, True) if action != "no_change" else before[:]
    return {"kind": "change-preview", "recipe": "family-thumbnail", "title": "Standardize family thumbnail",
            "keyColumn": "Setting", "columns": ["Setting"] + fields, "changedColumns": fields,
            "before": [before], "after": [after],
            "steps": [] if action == "no_change" else [{"action": action, "view_uid": existing["uid"] if existing else None}],
            "ownership_ids": [existing["id"]] if action == "reuse" else [],
            "notes": ["Review the saved thumbnail after Save As.",
                      "The view direction is isometric. Revit determines the saved image framing."],
            "warnings": [], "blocked": False, "basis": snapshot, "intent": intent, "proposed": desired}


def snapshot_from_record(record):
    response = record.get("response", record)
    if "result" in response:
        if response.get("status", "ok") != "ok" or response.get("error"):
            raise ValueError("The family query failed.")
        response = response["result"]
    if response.get("status") != "ok":
        raise ValueError("Read the staged family again before computing a preview.")
    return json.loads(response["snapshot_json"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("read")
    parser.add_argument("intent")
    args = parser.parse_args(argv)
    try:
        plan = review(snapshot_from_record(load(args.read)), load(args.intent))
        print(json.dumps(plan, ensure_ascii=True, allow_nan=False, indent=2))
        return 0
    except ValueError as error:
        print("Family preview stopped: " + str(error), file=sys.stderr)
    except (KeyError, TypeError, AttributeError, IOError):
        print("Family preview stopped. Check the snapshot and intent.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
