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


def checked_plan(plan, snapshot):
    if plan.get("blocked") is not False or any(item.get("blocking") for item in plan.get("warnings", [])):
        raise ValueError("Resolve the blocking warnings before changing the family.")
    if plan["basis"] != snapshot:
        raise ValueError("The staged family changed after preview. Read and preview again.")
    expected = review(snapshot, plan["intent"])
    fields = ("kind", "recipe", "proposed", "steps", "ownership_ids", "before", "after",
              "columns", "keyColumn", "changedColumns")
    if any(plan.get(key) != expected[key] for key in fields) or not expected["steps"]:
        raise ValueError("Use the reviewed preview with a real change.")
    return expected


def check_view_result(plan, identity, view, forced, require_force=True):
    desired = plan["proposed"]
    step = plan["steps"][0]
    if (identity != plan["basis"]["identity"] or not view or not decimal_id(view["id"]) or not view["uid"]
            or (step["action"] == "reuse" and view["uid"] != step["view_uid"])):
        raise ValueError("The thumbnail readback identifies a different family or view.")
    if (view["name"] != desired["name"] or view["view_type_id"] != desired["view_type"]["id"]
            or view["template"] or view["perspective"] or view["locked"]
            or view["crop_box"] or view["section_box"] or view["view_template_id"] != "-1"
            or view["display_style"] != desired["display_style"] or view["background"] != desired["background"]
            or (require_force and forced is not True) or not view["saved_orientation"]
            or not all(close_values(desired["orientation"][key], view["saved_orientation"][key])
                       for key in ("eye_ft", "forward", "up"))):
        raise ValueError("The saved thumbnail view settings differ from the preview.")


def runtime_result(record):
    response = record.get("response", record)
    if "result" in response:
        if response.get("status", "ok") != "ok" or response.get("error"):
            raise ValueError("The family operation failed.")
        response = response["result"]
    if response.get("status") != "ok":
        raise ValueError("The family operation did not produce a successful result.")
    return response


def verify_run(plan, record, kept=False):
    envelope = record.get("response", record)
    expected = ("modify", "committed") if kept else ("dry_run", "rolled_back")
    approvals = ("user", "auto") if kept else (None,)
    if ((envelope.get("mode"), envelope.get("decision")) != expected
            or envelope.get("approval") not in approvals or not envelope.get("run_id")
            or any(envelope.get(key) for key in ("reckless", "failures", "dialogs", "blocked", "closed_documents",
                                                 "unclosed_documents"))):
        raise ValueError("The guarded pyRevit record does not establish the requested outcome.")
    response = runtime_result(record)
    receipt = json.loads(response["receipt_json"])
    check_view_result(plan, receipt["identity"], receipt["preview_view"], receipt["force_preview_update"])
    changes = envelope.get("changes") or response.get("changes") or {}
    view_id = receipt["preview_view"]["id"]
    created = plan["steps"][0]["action"] == "create"
    added = [str(value) for value in changes.get("added_ids", [row["id"] for row in changes.get("added", [])])]
    modified = [str(row["id"]) for row in changes.get("modified", [])]
    categories = changes.get("by_category")
    if (changes.get("added_count") != int(created) or changes.get("deleted_count") != 0
            or changes.get("other_documents") or changes.get("deleted") != []
            or added != ([view_id] if created else [])
            or type(changes.get("modified_count")) is not int or len(modified) != changes["modified_count"]
            or modified not in ([], [view_id]) or not isinstance(categories, dict)
            or any(type(value) is not int or value < 0 for value in categories.values())
            or sum(categories.values()) != len(added) + len(modified)):
        raise ValueError("The runtime changes do not match the reviewed view scope.")
    return receipt, len(added), len(modified)


def verify_saved(plan, actual, output, kept):
    receipt, _added, _modified = verify_run(plan, kept, kept=True)
    basis = plan["basis"]["identity"]
    saved_input = output["revit_inputs"]
    saved_identity = actual["identity"]
    if (actual["kind"] != "family-preview-saved-snapshot" or actual["document_modified"] is not False
            or output["kind"] != "family-preview-saved-output" or output["status"] != "saved_unverified"
            or output["source_sha256"] != basis["staged_file_sha256"]
            or output["staged_path_sha256"] != basis["staged_path_sha256"]
            or saved_input["saved_path_sha256"] == basis["staged_path_sha256"]
            or saved_identity != {"family_uid": basis["family_uid"],
                                  "saved_path_sha256": saved_input["saved_path_sha256"],
                                  "saved_file_sha256": output["saved_sha256"]}
            or saved_input["saved_file_sha256"] != output["saved_sha256"]):
        raise ValueError("The reopened output does not match the staged family and saved-file evidence.")
    check_view_result(plan, basis, actual["preview_view"], actual["force_preview_update"], require_force=False)
    if actual["preview_view"]["uid"] != receipt["preview_view"]["uid"]:
        raise ValueError("The saved thumbnail view is not the kept view.")
    thumbnail = output["saved_thumbnail"]
    if (output["thumbnail_status"] not in ("changed", "unchanged", "present") or not thumbnail
            or not isinstance(thumbnail.get("sha256"), TEXT) or not SHA.match(thumbnail["sha256"])):
        raise ValueError("The saved family has no verified embedded PNG thumbnail.")
    return {"kind": "family-preview-saved-verification", "status": "thumbnail_review_pending",
            "thumbnail_status": output["thumbnail_status"], "thumbnail_sha256": thumbnail["sha256"],
            "reuse_receipt": {"family_uid": saved_identity["family_uid"],
                              "view_uid": actual["preview_view"]["uid"],
                              "saved_file_sha256": output["saved_sha256"]}}


def snapshot_from_record(record):
    return json.loads(runtime_result(record)["snapshot_json"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("read")
    parser.add_argument("intent")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--baseline", action="store_true")
    checks.add_argument("--verify-run", action="store_true")
    checks.add_argument("--verify-kept", metavar="KEPT_RUN")
    checks.add_argument("--verify-saved", nargs=2, metavar=("SAVED_FILE", "KEPT_RUN"))
    args = parser.parse_args(argv)
    try:
        evidence, supplied = load(args.read), load(args.intent)
        if args.baseline:
            checked_plan(supplied, snapshot_from_record(evidence))
            print("The staged family matches the reviewed baseline.")
            return 0
        if args.verify_run:
            checked_plan(supplied, supplied["basis"])
            _receipt, added, modified = verify_run(supplied, evidence)
            print("Verified the thumbnail settings and runtime scope (%d added, %d modified)." % (added, modified))
            return 0
        if args.verify_kept:
            checked_plan(supplied, supplied["basis"])
            kept_run = load(args.verify_kept)
            receipt, _added, _modified = verify_run(supplied, kept_run, kept=True)
            fresh = evidence.get("response", evidence)
            kept = kept_run.get("response", kept_run)
            if fresh.get("mode") != "query" or not fresh.get("run_id") or fresh["run_id"] == kept["run_id"]:
                raise ValueError("Read the kept view again with a separate Revit query.")
            actual = snapshot_from_record(evidence)
            check_view_result(supplied, actual["identity"], actual["preview_view"], actual["force_preview_update"])
            if actual["preview_view"]["uid"] != receipt["preview_view"]["uid"]:
                raise ValueError("The independently read view does not match the kept run.")
            print("The kept thumbnail view matches the reviewed settings.")
            return 0
        if args.verify_saved:
            checked_plan(supplied, supplied["basis"])
            fresh = evidence.get("response", evidence)
            kept_run = load(args.verify_saved[1])
            kept = kept_run.get("response", kept_run)
            if fresh.get("mode") != "query" or not fresh.get("run_id") or fresh["run_id"] == kept["run_id"]:
                raise ValueError("Read the reopened output in a separate Revit query.")
            result = verify_saved(supplied, snapshot_from_record(evidence), load(args.verify_saved[0]), kept_run)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        plan = review(snapshot_from_record(evidence), supplied)
        print(json.dumps(plan, ensure_ascii=True, allow_nan=False, indent=2))
        return 0
    except ValueError as error:
        print("Family preview stopped: " + str(error), file=sys.stderr)
    except (KeyError, TypeError, AttributeError, IOError):
        print("Family preview stopped. Check the snapshot and intent.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
