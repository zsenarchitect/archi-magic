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
TOKEN = re.compile(r"\{(name|index|id)\}|[{}]")


def element_id(value):
    if not isinstance(value, TEXT) or not ID.match(value) or int(value) > 2**63 - 1:
        raise ValueError("Expected a positive decimal Revit element ID.")
    return value


def duplicate_name(pattern, name, index, source_id):
    if not isinstance(pattern, TEXT) or not 1 <= len(pattern) <= 200:
        raise ValueError("Use a name pattern of one to 200 characters.")
    values = {"name": name, "index": str(index), "id": element_id(source_id)}

    def substitute(match):
        token = match.group(1)
        if token is None:
            raise ValueError("The only supported placeholders are {name}, {index}, and {id}.")
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
    marker_notes = []

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
        name = duplicate_name(request["pattern"], view["name"], index, source_id)
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
            members = families[primary_id]["members"]
            if source_id not in members:
                raise ValueError("The source view is missing from its dependent family.")
            for target_id in changes.get(primary_id, [primary_id]):
                if members[target_id]["writable"] is not True:
                    block(row, "Worksharing status blocks view %s." % target_id)

        before.append([row, view["name"], mode, None, None, None, None])
        after.append([row, view["name"], mode, name, template_name, None, None])
        step = {"source_id": source_id, "source_uid": view["uid"], "mode": mode,
                "name": name, "primary_id": primary_id}
        if "marker_host_id" in source:
            if mode != "dependent":
                raise ValueError("A marker host is only valid for a dependent duplicate.")
            host_id = element_id(source["marker_host_id"])
            host = snapshot["marker_hosts"][host_id]
            if host["writable"] is not True:
                block(row, "Worksharing status blocks the marker host %s." % host_id)
            owners.add(host_id)
            step["marker_host_id"] = host_id
            marker_notes.append(u"Review marker host %s (view %s) for %s." % (host["name"], host_id, name))
        steps.append(step)

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
    if marker_notes:
        display["notes"] = marker_notes
    return plan, display


def read_record(path):
    with io.open(path, "r", encoding="utf-8-sig") as stream:
        raw = stream.read(MAX_BYTES + 65537)
    if len(raw) > MAX_BYTES + 65536:
        raise ValueError("The runtime record is too large.")
    return json.loads(raw)


def read_payload(record, field):
    result = record["result"]
    if result["status"] != "ok":
        raise ValueError("Revit did not return a successful view snapshot.")
    payload = result[field]
    if len(payload) > MAX_BYTES or any(ord(char) > 127 for char in payload):
        raise ValueError("Expected an ASCII Revit payload of at most 2 MiB.")
    return json.loads(payload)


def read_snapshot(path):
    return read_payload(read_record(path), "snapshot_json")


def check_receipt(plan, receipt):
    if (receipt["basis_sha256"] != plan["basis_sha256"] or receipt["document"] != plan["document"]
            or plan["blocked"] or len(receipt["views"]) != len(plan["steps"])):
        raise ValueError("The dry run did not execute the approved batch.")
    for row, step in zip(receipt["views"], plan["steps"]):
        if (any(row[field] != step[field] for field in ("name", "mode", "primary_id"))
                or row["template_id"] != plan["request"]["template_id"] or not row["uid"]):
            raise ValueError("A dry-run view differs from the approved batch.")
    views = {row["id"]: (row, step) for row, step in zip(receipt["views"], plan["steps"])}
    if len(views) != len(plan["steps"]):
        raise ValueError("The receipt repeats a created view ID.")
    for detail in receipt["details"]:
        if views[detail["view_id"]][1]["mode"] != "detailing" or not detail["uid"]:
            raise ValueError("A copied detail does not belong to a reviewed detailing duplicate.")
    for marker in receipt["markers"]:
        step = views[marker["view_id"]][1]
        if step["mode"] != "dependent" or marker["host_id"] != step.get("marker_host_id") or not marker["uid"]:
            raise ValueError("A marker does not belong to a reviewed host and dependent view.")
    for primary_id, ids in plan["family_changes"].items():
        for item in ids:
            family = receipt["families"][item]
            expected_primary = "-1" if item == primary_id else primary_id
            if family["template_id"] != plan["request"]["template_id"] or family["primary_id"] != expected_primary:
                raise ValueError("An existing dependent family view differs from the approved batch.")


def require_rollback_record(record, mode):
    envelope = record.get("response", record)
    run_id, exit_code = envelope.get("run_id"), envelope.get("exit_code")
    if ((envelope.get("mode"), envelope.get("status"), envelope.get("decision")) != (mode, "ok", "rolled_back")
            or not isinstance(run_id, TEXT) or not run_id.strip() or envelope.get("result_truncated") is not False
            or type(exit_code) is not int or exit_code != 0 or envelope.get("approval") is not None
            or any(envelope.get(field) for field in ("error", "failures", "warnings", "blocked", "dialogs",
                                                     "unclosed_documents"))):
        raise ValueError("Use a complete Revit %s record that successfully rolled back." % mode)
    return envelope


def verify_dry_run(plan, record):
    record = require_rollback_record(record, "dry_run")
    receipt = read_payload(record, "receipt_json")
    check_receipt(plan, receipt)
    changes = record["changes"]
    added = [row["id"] for row in changes.get("added", [])]
    legacy_added = changes.get("added_ids")
    if legacy_added is not None:
        if "added" in changes and sorted(map(str, added)) != sorted(map(str, legacy_added)):
            raise ValueError("The runtime change record contains conflicting addition lists.")
        added = legacy_added
    added = sorted((element_id(str(item)) for item in added), key=int)
    modified = sorted((element_id(str(row["id"])) for row in changes.get("modified", [])), key=int)
    reported = receipt["changes"]
    categories = changes["by_category"]
    if (any(type(changes.get(field)) is not int for field in ("added_count", "modified_count", "deleted_count"))
            or changes["added_count"] != len(added) or changes["modified_count"] != len(modified)
            or len(set(added)) != len(added) or len(set(modified)) != len(modified) or set(added) & set(modified)
            or changes["deleted_count"] != 0 or any(changes.get(field) for field in
                ("deleted", "deleted_ids", "modified_ids", "other_documents"))
            or any(type(value) is not int or value <= 0 for value in categories.values())
            or sum(categories.values()) != len(added) + len(modified)
            or added != reported["added"] or modified != reported["modified"] or reported["deleted"]
            or not set(modified).issubset(plan["ownership_ids"])):
        raise ValueError("The complete runtime change record disagrees with the duplication receipt.")
    created = [row["id"] for row in receipt["views"]]
    details = [row["id"] for row in receipt["details"]]
    markers = [row["id"] for row in receipt["markers"]]
    if sorted(created + details + markers, key=int) != added:
        raise ValueError("The dry run reported an unexplained addition.")
    return receipt


def repeatable_state(receipt):
    index = {row["id"]: position for position, row in enumerate(receipt["views"])}
    views = [[row[field] for field in ("name", "mode", "primary_id", "view_type", "template_id")]
             for row in receipt["views"]]
    details = [[index[row["view_id"]], row["points"], row["style"]] for row in receipt["details"]]
    markers = [[index[row["view_id"]], row["host_id"], row["category_id"]] for row in receipt["markers"]]
    return {"views": views, "families": receipt["families"],
            "details": sorted(json.dumps(row, ensure_ascii=True) for row in details),
            "markers": sorted(markers), "modified": receipt["changes"]["modified"]}


def run_verification(command, plan_path, record_path, dry_run_path=None):
    plan = read_record(plan_path)
    if command == "baseline":
        snapshot = read_snapshot(record_path)
        if snapshot_digest(snapshot) != plan["basis_sha256"]:
            raise ValueError("The original Revit model state differs from the approved baseline.")
        print("The original Revit model matches the approved baseline.")
    elif command == "compare":
        if not dry_run_path:
            raise ValueError("Supply the full dry-run record for comparison.")
        query = require_rollback_record(read_record(record_path), "query")
        dry_record = require_rollback_record(read_record(dry_run_path), "dry_run")
        changes = query["changes"]
        if (query["run_id"] == dry_record["run_id"]
                or any(type(changes.get(field)) is not int or changes[field] != 0
                       for field in ("added_count", "modified_count", "deleted_count"))
                or any(changes.get(field) for field in ("added", "modified", "deleted", "added_ids", "modified_ids",
                                                        "deleted_ids", "by_category", "other_documents"))):
            raise ValueError("Use a distinct read-only Revit query to verify the kept batch.")
        receipt = read_payload(query, "verification_json")
        check_receipt(plan, receipt)
        dry = verify_dry_run(plan, dry_record)
        if repeatable_state(receipt) != repeatable_state(dry):
            raise ValueError("The independently verified kept batch differs from the approved dry run.")
        print("The independently verified kept batch matches the approved dry run.")
    else:
        receipt = verify_dry_run(plan, read_record(record_path))
        print("Created %d views, %d detail lines and %d dependent-view markers. Changed %d reviewed existing elements."
              % (len(receipt["views"]), len(receipt["details"]), len(receipt["markers"]),
                 len(receipt["changes"]["modified"])))
        for marker in receipt["markers"]:
            print("Marker %s appears in host view %s and points to new view %s."
                  % (marker["id"], marker["host_id"], marker["view_id"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", help="Runtime read record, baseline, compare, or side-effects")
    parser.add_argument("second", help="Plan output or approved plan")
    parser.add_argument("third", help="Preview output or runtime record")
    parser.add_argument("--dry-run", help="Full runtime dry-run record for compare")
    args = parser.parse_args()
    try:
        if args.first in ("baseline", "compare", "side-effects"):
            run_verification(args.first, args.second, args.third, args.dry_run)
            return 0
        plan, display = compute(read_snapshot(args.first))
        for path, data in [(args.second, plan), (args.third, display)]:
            with io.open(path, "w", encoding="utf-8") as stream:
                stream.write(u"%s\n" % json.dumps(data, ensure_ascii=True, indent=2))
        return 1 if plan["blocked"] else 0
    except (ValueError, KeyError, TypeError, AttributeError, IOError) as error:
        print("View duplication stopped: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
