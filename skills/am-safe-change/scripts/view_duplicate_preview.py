# -*- coding: utf-8 -*-
"""Plan a batch of view duplicates from a complete Revit snapshot."""
from __future__ import print_function

import argparse
import hashlib
import io
import json
import re
import sys

MAX_BYTES = 2 * 1024 * 1024
MAX_VIEWS = 10000
MAX_SOURCES = 50
MAX_FAMILY = 100
MODES = ("plain", "detailing", "dependent")
TEXT = (type(u""), str)
ID = re.compile(r"^[1-9][0-9]{0,18}\Z")
TOKEN = re.compile(r"\{(name|index)\}|[{}]")


def element_id(value):
    if not isinstance(value, TEXT) or not ID.match(value) or int(value) > 2**63 - 1:
        raise ValueError("Expected a positive decimal Revit element ID.")
    return value


def duplicate_name(pattern, name, index):
    if not isinstance(pattern, TEXT) or not 1 <= len(pattern) <= 200:
        raise ValueError("Use a name pattern of one to 200 characters.")
    values = {"name": name, "index": str(index)}

    def substitute(match):
        token = match.group(1)
        if token is None:
            raise ValueError("The only supported placeholders are {name} and {index}.")
        return values[token]

    result = TOKEN.sub(substitute, pattern)
    if (not result or result != result.strip() or len(result) > 100
            or any(ord(char) < 32 or ord(char) == 127 for char in result)):
        raise ValueError("Generated names must contain one to 100 display characters without outer whitespace.")
    return result


def snapshot_digest(snapshot):
    data = json.dumps(snapshot, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(data) > MAX_BYTES:
        raise ValueError("The Revit snapshot exceeds 2 MiB.")
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def compute(snapshot):
    if snapshot["kind"] != "view-duplicate-snapshot" or snapshot.get("complete") is not True:
        raise ValueError("Expected a complete view duplication snapshot.")
    basis = snapshot_digest(snapshot)
    request = snapshot["request"]
    template = snapshot["template"]
    template_id = element_id(request["template_id"])
    if template_id != template["id"] or not template["uid"] or not template["name"]:
        raise ValueError("The requested view template could not be verified.")
    sources = request["sources"]
    names = snapshot["names"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError("Choose one to 50 views to duplicate.")
    if not isinstance(names, list) or len(names) > MAX_VIEWS:
        raise ValueError("The complete existing-name inventory exceeds 10,000 views.")
    existing_names = {name.upper() for name in names}
    used_names = set(existing_names)
    before, after, warnings, steps = [], [], [], []
    families, changes, owners = {}, {}, set()
    template_name = template["name"]

    def block(row, message):
        warnings.append({"row": row, "text": message, "blocking": True})

    for index, source in enumerate(sources, 1):
        source_id = element_id(source["id"])
        mode = source["mode"]
        if mode not in MODES:
            raise ValueError("Choose plain, detailing, or dependent duplication.")
        view = snapshot["sources"][source_id]
        if not view["uid"] or view["name"].upper() not in existing_names:
            raise ValueError("A source view is missing from the current view inventory.")
        row = "view:%d" % index
        name = duplicate_name(request["pattern"], view["name"], index)
        name_key = name.upper()
        if name_key in used_names:
            block(row, "The proposed name is already in use.")
        used_names.add(name_key)
        if mode not in view["allowed_modes"]:
            block(row, "Revit does not allow the requested duplication mode for this view.")
        if view["template_allowed"] is not True:
            block(row, "The template is incompatible with this view.")
        if mode == "detailing" and view.get("details_complete") is not True:
            block(row, "Some source annotations cannot be fully verified.")

        primary_id = "-1"
        if mode == "dependent":
            primary_id = element_id(view["primary_id"] if view["primary_id"] != "-1" else source_id)
            owners.add(primary_id)
            if primary_id not in families:
                family = snapshot["families"][primary_id]
                members = family["members"]
                if primary_id not in members or not 1 <= len(members) <= MAX_FAMILY:
                    raise ValueError("The complete dependent family could not be verified.")
                families[primary_id] = family
                if any(member["template_id"] != template_id for member in members.values()):
                    if family["template_allowed"] is not True:
                        block(row, "The template is incompatible with the primary view.")
                    if request.get("allow_family_template_change") is not True:
                        block(row, "Changing the existing dependent family requires explicit approval.")
                    changes[primary_id] = sorted(members, key=int)
                    owners.update(members)
            if source_id not in families[primary_id]["members"]:
                raise ValueError("The source view is missing from its dependent family.")

        before.append([row, view["name"], mode, None, None, None, None])
        after.append([row, view["name"], mode, name, template_name, None, None])
        steps.append({"source_id": source_id, "source_uid": view["uid"], "mode": mode,
                      "name": name, "primary_id": primary_id})

    if sum(len(family["members"]) for family in families.values()) > MAX_FAMILY:
        raise ValueError("The batch exceeds 100 existing dependent family views.")
    for primary_id in sorted(changes, key=int):
        for member_id in changes[primary_id]:
            member = families[primary_id]["members"][member_id]
            row = "family:" + member_id
            placements = u", ".join(member["sheets"])
            settings = u", ".join(template["controlled_settings"])
            before.append([row, member["name"], None, member["name"], member["template_name"], placements,
                           u", ".join(member["controlled_settings"])])
            after.append([row, member["name"], None, member["name"], template_name, placements, settings])

    blocked = bool(warnings)
    ownership_ids = sorted(owners, key=int)
    plan = {"kind": "view-duplicate-plan", "basis_sha256": basis, "request": request,
            "document": snapshot["document"], "blocked": blocked, "steps": [] if blocked else steps,
            "family_changes": {} if blocked else changes, "ownership_ids": ownership_ids}
    display = {"kind": "change-preview", "recipe": "view-duplicate", "title": "Duplicate Revit views",
               "basis_sha256": basis, "keyColumn": "Row", "columns": [
                   "Row", "View", "Mode", "Result name", "Template", "Sheets", "Controlled settings"],
               "changedColumns": ["Result name", "Template", "Controlled settings"],
               "before": before, "after": after, "warnings": warnings, "blocked": blocked,
               "ownership_ids": ownership_ids, "steps": [], "review_only": True}
    return plan, display


def read_snapshot(path):
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        raw = stream.read(MAX_BYTES + 65537)
    if len(raw) > MAX_BYTES + 65536:
        raise ValueError("The runtime record is too large.")
    record = json.loads(raw)
    result = record["result"]
    if result["status"] != "ok":
        raise ValueError("Revit did not return a successful view snapshot.")
    payload = result["snapshot_json"]
    if len(payload) > MAX_BYTES or any(ord(char) > 127 for char in payload):
        raise ValueError("Expected an ASCII Revit snapshot of at most 2 MiB.")
    return json.loads(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record")
    parser.add_argument("plan")
    parser.add_argument("preview")
    args = parser.parse_args()
    try:
        plan, display = compute(read_snapshot(args.record))
        for path, data in [(args.plan, plan), (args.preview, display)]:
            with io.open(path, "w", encoding="utf-8") as stream:
                stream.write(u"%s\n" % json.dumps(data, ensure_ascii=True, indent=2))
        return 1 if plan["blocked"] else 0
    except (ValueError, KeyError, TypeError, AttributeError, IOError) as error:
        print("View duplication stopped: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
