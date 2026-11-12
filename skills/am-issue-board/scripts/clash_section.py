# -*- coding: utf-8 -*-
"""Pure Navisworks clash mapping and section-view preview."""
import binascii
import hashlib
import json
import math
import re
import struct
import uuid

FRAME = "revit_host_internal_feet"


def snapshot_sha256(snapshot):
    # The preview and writer run under different Python engines. Hash the
    # float bits so JSON formatting of exponents and precision cannot drift.
    def canonical(value):
        if type(value) is float:
            if math.isnan(value) or math.isinf(value):
                raise ValueError("The clash snapshot contains a nonfinite value.")
            return ["float64", binascii.hexlify(struct.pack(">d", value)).decode("ascii")]
        if isinstance(value, dict):
            return ["object", [[key, canonical(value[key])] for key in sorted(value)]]
        if isinstance(value, list):
            return ["array", [canonical(item) for item in value]]
        return value

    basis = json.dumps(canonical(snapshot), ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(basis.encode("ascii")).hexdigest()


def text(value):
    if not isinstance(value, (str, type(u""))) or len(value) > 500:
        raise ValueError("Report text exceeds the supported limit.")
    return value.strip()


def point(value):
    if (not isinstance(value, list) or len(value) != 3
            or any(type(n) not in (int, float) or not -1e6 <= n <= 1e6 for n in value)):
        raise ValueError("Bounds must contain three finite host coordinates.")
    return value


def element_snapshot(doc):
    if doc["coordinate_frame"] != FRAME or not text(doc["host_id"]):
        raise ValueError("Use a named Revit host snapshot in internal feet.")
    sources = doc["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 32:
        raise ValueError("Use 1 to 32 source model instances.")
    index = {}
    ids = set()
    total = 0
    for source in sources:
        model = text(source["model_id"])
        prefix = source["path_prefix"]
        if not isinstance(prefix, list) or not 1 <= len(prefix) <= 20:
            raise ValueError("A source needs an exact selection-tree path prefix.")
        prefix = tuple(text(node) for node in prefix)
        if not model or not all(prefix) or prefix in index or model in ids:
            raise ValueError("Duplicate or empty source model mapping.")
        ids.add(model)
        entries = source["elements"]
        if not isinstance(entries, dict):
            raise ValueError("Use an element ID dictionary.")
        total += len(entries)
        if total > 10000:
            raise ValueError("Element snapshot exceeds the supported count.")
        elements = {}
        unique = set()
        for key, item in entries.items():
            low, high = point(item["min"]), point(item["max"])
            uid = text(item["unique_id"])
            if (not re.match(r"^[1-9][0-9]{0,18}\Z", key) or int(key) > 2**63 - 1
                    or not uid or uid in unique or not all(0 <= b - a <= 1000 for a, b in zip(low, high))
                    or low == high):
                raise ValueError("Invalid or duplicate element identity or bounds.")
            unique.add(uid)
            elements[key] = {"model_id": model, "unique_id": uid, "min": low, "max": high}
        index[prefix] = elements
    return doc["host_id"], index


def mapped_elements(objects, index):
    if len(objects) != 2:
        return None
    targets = []
    for item in objects:
        path = tuple(item["selection_path"])
        matches = [elements for prefix, elements in index.items() if path[:len(prefix)] == prefix]
        if len(matches) != 1 or item["element_id"] not in matches[0]:
            return None
        targets.append(matches[0][item["element_id"]])
    if (targets[0]["model_id"], targets[0]["unique_id"]) == (targets[1]["model_id"], targets[1]["unique_id"]):
        return None
    return targets


def section_box(objects, index, padding):
    targets = mapped_elements(objects, index)
    if targets is None:
        return None
    low = [min(a, b) - padding for a, b in zip(targets[0]["min"], targets[1]["min"])]
    high = [max(a, b) + padding for a, b in zip(targets[0]["max"], targets[1]["max"])]
    if not all(-1e6 <= a < b <= 1e6 and b - a <= 1000 for a, b in zip(low, high)):
        return None
    return {"min": low, "max": high, "targets": [
        {key: item[key] for key in ("model_id", "unique_id")} for item in targets
    ]}


def section_view_preview(guid, clashes, snapshot, padding, report_sha256):
    if str(uuid.UUID(str(guid))) != guid:
        raise ValueError("Choose one canonical clash GUID.")
    if type(padding) not in (float, int) or not 0.1 <= padding <= 20:
        raise ValueError("Use padding from 0.1 to 20 feet.")
    if not re.match(r"^[0-9a-f]{64}\Z", report_sha256):
        raise ValueError("The source report digest is unavailable.")
    selected = [clash for clash in clashes if clash["guid"] == guid]
    if len(selected) != 1 or snapshot["kind"] != "navisworks-revit-snapshot":
        raise ValueError("Use one XML clash and its Revit section-view snapshot.")
    clash = selected[0]
    target = snapshot["view_target"]
    name = "Clash " + guid
    if target["clash_guid"] != guid or target["name"] != name or not text(target["view_type_uid"]):
        raise ValueError("The snapshot is for a different clash or view type.")
    _, index = element_snapshot(snapshot)
    mapped = mapped_elements(clash["objects"], index)
    box = section_box(clash["objects"], index, padding)
    warnings = []
    if not box:
        warnings.append({"row": "view", "text": "Both exact Revit targets need verified bounds.", "blocking": True})
    if target["default_template_id"] != "-1":
        warnings.append({"row": "view", "text": "Choose a 3D view type without a default template.", "blocking": True})
    if target["name_conflicts"]:
        warnings.append({"row": "view", "text": "A clash view name is already in use. Verify the existing view before retrying.",
                         "blocking": True})
    sources = {item["model_id"]: item for item in snapshot["sources"]}
    rows = []
    targets = []
    for position, (source_object, element) in enumerate(zip(clash["objects"], mapped or []), 1):
        source = sources[element["model_id"]]
        if not source["document_identity"]:
            raise ValueError("A mapped source does not have a saved document identity.")
        targets.append({"model_id": element["model_id"], "element_id": source_object["element_id"],
                        "element_uid": element["unique_id"], "link_instance_id": source["link_instance_id"],
                        "link_instance_uid": source["link_instance_uid"],
                        "document_identity": source["document_identity"], "min": element["min"], "max": element["max"]})
        rows.append(["target:%d" % position, "Clash element", element["model_id"],
                     source_object["element_id"], element["min"], element["max"]])
    view = ["view", name, None, None, box["min"] if box else None, box["max"] if box else None]
    blocked = bool(warnings)
    digest = snapshot_sha256(snapshot)
    plan = {"kind": "navisworks-section-view-plan", "basis_sha256": digest, "report_sha256": report_sha256,
            "host_identity": snapshot["host_identity"], "active_view_uid": snapshot["active_view_uid"],
            "request": snapshot["request"], "view": target, "targets": targets, "box": box,
            "clash_objects": clash["objects"],
            "padding_ft": padding, "blocked": blocked, "steps": [] if blocked else [{"action": "create_view"}]}
    display = {"kind": "change-preview", "recipe": "navisworks-section-view", "title": "Create a Navisworks clash view",
               "basis_sha256": digest, "keyColumn": "Row",
               "columns": ["Row", "View or target", "Source", "Element ID", "Minimum ft", "Maximum ft"],
               "changedColumns": ["Minimum ft", "Maximum ft"], "before": rows, "after": rows + [view],
               "notes": ["Clash %s. Section box padding %.3f ft." % (guid, padding),
                         "Verify both target elements in the kept view before recording view availability."],
               "warnings": warnings, "blocked": blocked, "steps": [], "ownership_ids": [], "review_only": True}
    return plan, display
