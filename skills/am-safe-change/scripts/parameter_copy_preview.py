# -*- coding: utf-8 -*-
"""Compute a category-scoped parameter copy preview. Runs without Revit."""
from __future__ import print_function

import argparse
import hashlib
import io
import json
import math
import os
import re
import sys

try:
    TEXT = basestring
    INTEGER = (int, long)
except NameError:
    TEXT = str
    INTEGER = (int,)

MAX_ELEMENTS = 500
MAX_SEGMENTS = 16
MAX_TEXT = 2000
MAX_SNAPSHOT = 512 * 1024
MAX_SCAN = 5000
SCAN_SECONDS = 10


def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def element_id(value):
    if not isinstance(value, TEXT) or not re.match(r"[1-9][0-9]{0,18}\Z", value) or int(value) >= 2 ** 63:
        raise ValueError("Use a positive decimal element ID string.")
    return value


def parameter_name(value):
    if not isinstance(value, TEXT) or not value.strip() or value != value.strip() or len(value) > MAX_TEXT:
        raise ValueError("Use a nonempty exact parameter name.")
    return value


def normalize_request(request):
    if not isinstance(request, dict) or set(request) - {"category_id", "element_ids", "destination", "source", "parts"}:
        raise ValueError("Specify a category, destination and either a source or combination.")
    category_id = request["category_id"]
    if not isinstance(category_id, TEXT) or not re.match(r"-[1-9][0-9]{0,9}\Z", category_id) or int(category_id) < -(2 ** 31):
        raise ValueError("Use a negative built-in category ID string.")
    result = {"category_id": category_id, "destination": parameter_name(request["destination"])}
    if "element_ids" in request:
        ids = request["element_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ELEMENTS:
            raise ValueError("Select 1 to 500 element IDs.")
        ids = [element_id(value) for value in ids]
        if len(set(ids)) != len(ids):
            raise ValueError("Use unique element IDs.")
        result["element_ids"] = sorted(ids, key=int)
    if ("source" in request) == ("parts" in request):
        raise ValueError("Choose exactly one source or combination.")
    if "source" in request:
        result["source"] = parameter_name(request["source"])
        return result
    parts = request["parts"]
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_SEGMENTS:
        raise ValueError("Use 1 to 16 combination parts.")
    fields = []
    for part in parts:
        if isinstance(part, dict) and set(part) == {"parameter"}:
            fields.append(parameter_name(part["parameter"]))
        elif (not isinstance(part, dict) or set(part) != {"literal"}
              or not isinstance(part["literal"], TEXT) or len(part["literal"]) > MAX_TEXT):
            raise ValueError("Each combination part must specify one parameter or literal.")
    if not fields:
        raise ValueError("Include at least one parameter in a combination.")
    result["parts"] = parts
    return result


def field_names(request):
    sources = [request["source"]] if "source" in request else [
        part["parameter"] for part in request["parts"] if "parameter" in part]
    return sorted(set(sources + [request["destination"]]))


def typed_value(field, name):
    if not field["has_value"]:
        raise ValueError(u"Source parameter is unset: " + name)
    storage, value = field["storage"], field["value"]
    if storage == "String":
        valid = isinstance(value, TEXT) and len(value) <= MAX_TEXT and u"\x00" not in value
    elif storage == "Integer":
        valid = type(value) in INTEGER and -(2 ** 31) <= value < 2 ** 31
    elif storage == "Double":
        valid = type(value) is float and not math.isnan(value) and not math.isinf(value)
    elif storage == "ElementId":
        valid = isinstance(value, dict) and bool(value.get("uid")) and bool(element_id(value.get("id")))
    else:
        valid = False
    if not valid:
        raise ValueError(u"Source has no supported typed value: " + name)
    return value


def proposed_value(fields, request, destination):
    if "source" in request:
        name = request["source"]
        source = fields[name]
        if (source["storage"] != destination["storage"] or not source["spec"]
                or source["spec"] != destination["spec"]):
            raise ValueError("Source and destination have different storage types or Revit data types.")
        if destination["storage"] == "ElementId" and not destination.get("supports_reference"):
            raise ValueError("This destination does not accept direct element reference copies.")
        return typed_value(source, name)
    if destination["storage"] != "String":
        raise ValueError("Combined text needs a text destination.")
    pieces = []
    for part in request["parts"]:
        if "literal" in part:
            pieces.append(part["literal"])
            continue
        name = part["parameter"]
        field = fields[name]
        value = typed_value(field, name)
        display = value if field["storage"] == "String" else field.get("display")
        if not isinstance(display, TEXT):
            raise ValueError(u"Revit has no display value for " + name)
        pieces.append(display)
    result = u"".join(pieces)
    if len(result) > MAX_TEXT or u"\x00" in result:
        raise ValueError("The combination must contain at most 2,000 characters and no NUL.")
    return result


def display_value(field, value):
    if value is None:
        return "(unset)"
    if field["storage"] == "String":
        return u'"%s"' % value if value else "(empty text)"
    if field["storage"] == "ElementId":
        return u"%s [ID %s]" % (value.get("label") or "Element", value["id"])
    return repr(value)


def compute(snapshot):
    request = normalize_request(snapshot["request"])
    if len(canonical(snapshot)) > MAX_SNAPSHOT:
        raise ValueError("The parameter snapshot exceeds 512 KiB.")
    rows = snapshot["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ELEMENTS:
        raise ValueError("The snapshot must have 1 to 500 elements.")
    rows = sorted(rows, key=lambda row: int(element_id(row["id"])))
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids) or ids != request.get("element_ids", ids):
        raise ValueError("The snapshot does not match the requested elements.")
    names = field_names(request)
    identity_sets = {
        name: {canonical(row["fields"][name]["identity"]) for row in rows
               if "identity" in row["fields"][name]} for name in names
    }

    before, after, steps, warnings, schemes = [], [], [], [], {}
    target = request["destination"]
    for row in rows:
        eid, fields = row["id"], row["fields"]
        scheme = row["scheme"]
        schemes[scheme] = schemes.get(scheme, 0) + 1
        destination = fields[target]
        old = destination.get("value") if destination.get("has_value") else None
        new = old
        try:
            for name in names:
                if "error" in fields[name]:
                    raise ValueError(u"%s: %s" % (name, fields[name]["error"]))
                if len(identity_sets[name]) != 1:
                    raise ValueError(u"Parameter name resolves to different definitions: " + name)
            if destination["read_only"] or destination["global_controlled"] or row["grouped"]:
                raise ValueError("Destination is read-only, globally controlled, or on a group member.")
            new = proposed_value(fields, request, destination)
            if old != new:
                steps.append({"id": eid, "uid": row["uid"], "identity": destination["identity"],
                              "storage": destination["storage"], "value": new})
        except ValueError as error:
            warnings.append({"row": eid, "text": error.args[0], "blocking": True})
        before.append([eid, display_value(destination, old)])
        after.append([eid, display_value(destination, new)])

    blocked = bool(warnings)
    digest = fingerprint(snapshot)
    preview = {
        "kind": "change-preview", "title": "Copy or combine instance parameter values",
        "keyColumn": "Element ID", "columns": ["Element ID", "Destination value"],
        "changedColumns": ["Destination value"] if steps else [], "before": before, "after": after,
        "notes": [u"Destination parameter: " + target,
                  "Blocked batches cannot be applied." if blocked else "Review the proposed values before the dry run."],
        "warnings": warnings, "blocked": blocked, "review_only": True, "steps": [],
        "ownership_ids": [step["id"] for step in steps] if not blocked else [],
        "parameter_copy_basis": digest,
    }
    plan = {
        "recipe": "parameter_copy", "request": request, "basis": digest,
        "project": snapshot.get("project"), "category": rows[0].get("category"),
        "baseline": {"category_id": request["category_id"], "schemes": schemes, "ids_sha256": fingerprint(ids)},
        "steps": [] if blocked else steps,
    }
    return {"preview": preview, "plan": plan}


def read_basis(data):
    envelope = data.get("response", data)
    if envelope.get("status", "ok") != "ok" or envelope.get("error") or envelope.get("result_truncated"):
        raise ValueError("Read the complete successful parameter snapshot.")
    result = envelope.get("result", envelope)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValueError("Read the complete successful parameter snapshot.")
    return json.loads(result["snapshot_json"])


def load(path):
    with io.open(path, encoding="utf-8-sig") as source:
        return json.load(source)


def compare_kept(plan, basis):
    if normalize_request(basis["request"]) != plan["request"] or basis["project"] != plan["project"]:
        raise ValueError("The kept read is from a different request or project.")
    rows = {row["id"]: row for row in basis["rows"]}
    destination = plan["request"]["destination"]
    for step in plan["steps"]:
        row = rows[step["id"]]
        field = row["fields"][destination]
        if (row["uid"] != step["uid"] or field.get("identity") != step["identity"]
                or field.get("storage") != step["storage"] or field.get("value") != step["value"]):
            raise ValueError("The kept parameter values differ from the approved result.")


def verify_dry_run(plan, saved):
    envelope = saved.get("response", saved)
    run_id = envelope.get("run_id")
    exit_code = envelope.get("exit_code")
    if (envelope.get("mode") != "dry_run" or envelope.get("status") != "ok"
            or envelope.get("decision") != "rolled_back" or envelope.get("approval") is not None
            or not isinstance(run_id, TEXT) or not run_id.strip()
            or type(exit_code) not in INTEGER or exit_code != 0 or envelope.get("result_truncated") is not False
            or any(envelope.get(key) for key in (
                "error", "blocked", "failures", "dialogs", "warnings", "closed_documents", "unclosed_documents"))):
        raise ValueError("Save the complete successful rolled-back dry run.")
    actual = envelope.get("result", {})
    if actual.get("status") != "ok" or json.loads(actual["rows_json"]) != plan["steps"]:
        raise ValueError("The dry-run values differ from the reviewed changes.")
    changes = envelope.get("changes") or {}
    modified = changes.get("modified", [])
    ids = {str(row["id"]) for row in modified}
    expected = {step["id"] for step in plan["steps"]}
    categories = changes.get("by_category")
    if (changes.get("added_count") != 0 or changes.get("deleted_count") != 0
            or changes.get("other_documents") or changes.get("modified_count") != len(ids)
            or len(ids) != len(modified) or not isinstance(categories, dict)
            or sum(categories.values()) != len(ids) or not expected.issubset(ids)):
        raise ValueError("Dry-run telemetry is incomplete or shows changes outside the recipe.")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
    import side_effects
    category = plan["category"]
    summary = side_effects.summarize(plan, {"modified_count": len(ids), "modified_by_category": categories},
                                     [category])
    print(side_effects.text_table(summary))
    if ids != expected or summary["unexpected"] or summary["warnings"]:
        raise ValueError("Review the additional modified elements: %s" % ", ".join(sorted(ids - expected, key=int)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compute", "baseline", "compare", "side-effects"))
    parser.add_argument("input")
    parser.add_argument("output_or_receipt")
    parser.add_argument("--preview", help="Also write the display preview (compute only).")
    args = parser.parse_args()
    try:
        if args.command == "compute":
            artifact = compute(read_basis(load(args.input)))
            with io.open(args.output_or_receipt, "w", encoding="utf-8") as output:
                output.write(canonical(artifact) + u"\n")
            if args.preview:
                with io.open(args.preview, "w", encoding="utf-8") as output:
                    output.write(canonical(artifact["preview"]) + u"\n")
            print("%s changed, %s warnings." % (len(artifact["plan"]["steps"]), len(artifact["preview"]["warnings"])))
        else:
            plan = load(args.input)["plan"]
            receipt = load(args.output_or_receipt)
            if args.command == "baseline":
                if fingerprint(read_basis(receipt)) != plan["basis"]:
                    raise ValueError("The selected parameters changed since the preview.")
                print("The original parameter state matches the preview.")
            elif args.command == "compare":
                compare_kept(plan, read_basis(receipt))
                print("The kept parameter values match the preview.")
            else:
                verify_dry_run(plan, receipt)
    except (ValueError, KeyError, TypeError, IOError, AttributeError, OverflowError) as error:
        print("Parameter copy stopped: %s" % error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
