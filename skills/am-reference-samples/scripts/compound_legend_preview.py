# -*- coding: utf-8 -*-
"""Validate compound wall and floor snapshots and render a layer legend."""
from __future__ import print_function

import argparse
import hashlib
import io
import json
import math
import re
import sys

try:
    TEXT = basestring
except NameError:
    TEXT = str

MM_PER_FOOT = 304.8
WIDTH_TOLERANCE_FT = 0.01 / MM_PER_FOOT
MAX_SCAN = 20000
MAX_TYPES = 100
MAX_LAYERS = 1000
MAX_BYTES = 1024 * 1024
MAX_SELECTED = 500
CATEGORIES = ("Wall", "Floor")


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def positive_id(value):
    if (not isinstance(value, TEXT) or not re.match(r"[1-9][0-9]{0,18}\Z", value)
            or int(value) >= 2 ** 63):
        raise ValueError("Use a positive decimal Revit element ID.")
    return value


def read_request(request):
    if not isinstance(request, dict) or set(request) - {"category", "element_ids", "view_name", "drafting_type_id", "text_type_id"}:
        raise ValueError("Use a wall/floor scope and optional drafting-view settings.")
    result = {}
    if "category" in request:
        if request["category"] not in CATEGORIES:
            raise ValueError("Choose Wall or Floor.")
        result["category"] = request["category"]
    if "element_ids" in request:
        ids = request["element_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_SELECTED:
            raise ValueError("Select 1 to 500 wall or floor IDs.")
        ids = [positive_id(value) for value in ids]
        if len(ids) != len(set(ids)):
            raise ValueError("Use unique selected element IDs.")
        result["element_ids"] = sorted(ids, key=int)
    settings = {"view_name", "drafting_type_id", "text_type_id"}
    if settings & set(request):
        if not settings.issubset(request):
            raise ValueError("Provide the view name, drafting type ID, and text type ID together.")
        name = request["view_name"]
        if not isinstance(name, TEXT) or not name.strip() or len(name) > 100 or name != name.strip():
            raise ValueError("Use a view name of 1 to 100 characters.")
        result.update(view_name=name, drafting_type_id=positive_id(request["drafting_type_id"]),
                      text_type_id=positive_id(request["text_type_id"]))
    return result


def width(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 100:
        raise ValueError("Invalid layer thickness or type width.")
    if math.isnan(value) or math.isinf(value):
        raise ValueError("A layer thickness or type width is not finite.")
    return value


def dimension(value):
    return "Unavailable" if value is None else "%.3f mm" % (value * MM_PER_FOOT)


def layer_sum(layers):
    if not isinstance(layers, list) or not layers:
        raise ValueError("A compound type must have layers.")
    return math.fsum(width(layer["width_ft"]) for layer in layers)


def compute(snapshot):
    request = read_request(snapshot["request"])
    if len(canonical(snapshot)) > MAX_BYTES:
        raise ValueError("The snapshot exceeds 1 MiB.")
    types = snapshot["types"]
    if not isinstance(types, list) or not 1 <= len(types) <= MAX_TYPES:
        raise ValueError("Read 1 to 100 used compound wall or floor types.")
    rows = sorted(types, key=lambda item: (item["category"], item["name"], int(positive_id(item["id"]))))
    if len({item["id"] for item in rows}) != len(rows):
        raise ValueError("A compound type appears more than once.")
    if sum(len(item["layers"]) + len(item.get("slice", {}).get("layers", [])) for item in rows) > MAX_LAYERS:
        raise ValueError("The snapshot exceeds 1,000 layer entries.")
    checks, warnings = [], []
    for item in rows:
        if item["category"] not in CATEGORIES or ("category" in request and item["category"] != request["category"]):
            raise ValueError("A type is outside the requested wall/floor scope.")
        if type(item["instances"]) is not int or not 1 <= item["instances"] <= MAX_SCAN:
            raise ValueError("A type has an invalid instance count.")
        nominal = item["width_ft"]
        if nominal is not None:
            width(nominal)
        nominal_sum = layer_sum(item["layers"])
        sample = item.get("slice")
        sample_width = width(sample["width_ft"]) if sample else nominal
        sample_layers = sample["layers"] if sample else item["layers"]
        sample_sum = layer_sum(sample_layers) if sample else nominal_sum
        if sample:
            width(item["grid_width_ft"])
            width(sample["height_ft"])
        problems = []
        if nominal is None:
            problems.append("Revit did not return an independent nominal type width.")
        if sample_width is None or abs(sample_sum - sample_width) > WIDTH_TOLERANCE_FT:
            problems.append("Layer sum and reference width do not match.")
        if sample_sum <= 0:
            problems.append("The section has no positive layer width.")
        if any(layer["material"] is None for layer in item["layers"] + (sample_layers if sample else [])):
            problems.append("One or more layer materials could not be resolved.")
        warnings.extend({"row": item["id"], "text": text, "blocking": True} for text in problems)
        checks.append(dict(item, nominal_sum_ft=nominal_sum, section_sum_ft=sample_sum,
                           section_reference_ft=sample_width, qualified=not problems))

    basis = fingerprint(snapshot)
    comparison_rows = [
        [item["id"], item["category"], item["name"], str(item["instances"]),
         dimension(item["width_ft"]), dimension(item["section_sum_ft"]),
         dimension(item["section_reference_ft"]), "Match" if item["qualified"] else "Review"]
        for item in checks
    ]
    shown = {
        "kind": "change-preview", "title": "Compound wall and floor legend", "keyColumn": "Element ID",
        "columns": ["Element ID", "Category", "Type", "Instances", "Nominal type width",
                    "Section layer sum", "Section reference width", "Width check"],
        "changedColumns": [], "before": [], "after": comparison_rows, "warnings": warnings,
        "blocked": bool(warnings), "review_only": True, "steps": [], "ownership_ids": [],
        "notes": ["Create one drafting view from the reviewed section and table for each listed type.",
                  "Types without compound layers: %d." % len(snapshot.get("excluded", []))],
        "compound_legend_basis": basis,
    }
    return {"preview": shown, "plan": {"recipe": "compound_type_legend", "basis": basis,
                                     "request": request, "types": checks, "target": snapshot.get("target"),
                                     "excluded": snapshot.get("excluded", [])}}


def render_html(artifact):
    from html import escape

    def text(value):
        return escape(str(value))

    def table(layers):
        rows = []
        for index, layer in enumerate(layers, 1):
            label = (layer["material"] or "Unresolved material " + layer["material_id"])
            label = text(label)
            flags = " · variable" if layer["variable"] else ""
            rows.append("<tr><td>%d</td><td>%s</td><td>%s</td><td>%s%s</td></tr>" %
                        (index, label, text(layer["function"]), dimension(layer["width_ft"]), flags))
        return ("<table><thead><tr><th>Layer</th><th>Material</th><th>Function</th><th>Thickness</th>"
                "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")

    types = artifact["plan"]["types"]
    maximum = max(item["section_sum_ft"] for item in types) or 1
    sections = []
    for item in types:
        sample = item.get("slice")
        layers = sample["layers"] if sample else item["layers"]
        orientation = "Exterior → Interior" if item["category"] == "Wall" else "Top → Bottom"
        axis = "width" if item["category"] == "Wall" else "height"
        parts, offset = [], 0.0
        for index, layer in enumerate(layers):
            if layer["width_ft"]:
                parts.append('<span class="layer tone%d" style="%s:%.9f%%"></span>' %
                             (index % 3, axis, 100 * layer["width_ft"] / maximum))
            else:
                edge = "left" if axis == "width" else "top"
                parts.append('<span class="membrane" style="%s:%.9f%%" title="Zero-thickness layer %d"></span>' %
                             (edge, 100 * offset / maximum, index + 1))
            offset += layer["width_ft"]
        details = ("<p>Sample at %s above the base. Nominal grid width %s. Nominal layer sum %s.</p>" %
                   (dimension(sample["height_ft"]), dimension(item["grid_width_ft"]),
                    dimension(item["nominal_sum_ft"]))) if sample else ""
        nominal = "<h3>Nominal layers</h3>" + table(item["layers"]) if sample else ""
        status = "Width check passed" if item["qualified"] else "Review the width or unresolved layers"
        sections.append('<section><h2>%s · %s</h2><p>%s instances · Type ID %s</p>'
                        '<p>Nominal type width %s · Section layer sum %s · Reference %s</p>'
                        '<p>%s</p>%s<p>%s</p><div class="diagram %s" aria-hidden="true">%s</div>'
                        '%s%s</section>' %
                        (text(item["category"]), text(item["name"]), item["instances"], item["id"],
                         dimension(item["width_ft"]), dimension(item["section_sum_ft"]),
                         dimension(item["section_reference_ft"]), status, details, orientation,
                         item["category"].lower(), "".join(parts), table(layers), nominal))
    request = artifact["plan"]["request"]
    scope = text(request.get("category", "Walls and floors"))
    if "element_ids" in request:
        scope += " · %d selected instances" % len(request["element_ids"])
    else:
        scope += " · host project"
    excluded = artifact["plan"]["excluded"]
    omitted = ""
    if excluded:
        omitted = "<h2>Types without compound layers</h2><ul>" + "".join(
            "<li>%s · %s</li>" % (text(item["category"]), text(item["name"])) for item in excluded) + "</ul>"
    return """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Compound wall and floor legend</title>
<style>
:root{color-scheme:light dark;font:1rem system-ui,sans-serif}
body{max-width:56rem;margin:2rem auto;padding:0 1rem}
section{border-top:1px solid GrayText;padding:1rem 0 2rem;break-inside:avoid}
h2,p{margin:.5rem 0}table{border-collapse:collapse;width:100%}
th,td{text-align:left;padding:.4rem;border-bottom:1px solid GrayText;overflow-wrap:anywhere}
.diagram{display:flex;position:relative;margin:1rem 0;max-width:30rem;height:2.5rem}
.floor{flex-direction:column;height:9rem;width:12rem}
.layer{flex:none;outline:1px solid CanvasText}.floor .layer{width:100%}
.tone0{background:#7a9cb8}.tone1{background:#aabb91}.tone2{background:#c89f77}
.membrane{position:absolute;height:100%;outline:1px dashed CanvasText}
.floor .membrane{width:100%;height:0}
@media print{.layer{print-color-adjust:exact}}
</style><h1>Compound wall and floor legend</h1>
<p>Type sections share a scale within each orientation. Width checks use unrounded source values.</p>
""" + "<p>Scope: " + scope + "</p>" + "\n".join(sections) + omitted + "</html>\n"


def load(path):
    with io.open(path, encoding="utf-8-sig") as source:
        return json.load(source)


def snapshot_from(record):
    result = record.get("result", record)
    if not isinstance(result, dict) or result.get("status") != "ok":
        raise ValueError("Read the complete successful compound type snapshot.")
    return json.loads(result["snapshot_json"])


def write(path, content):
    with io.open(path, "w", encoding="utf-8") as output:
        output.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record")
    parser.add_argument("artifact")
    parser.add_argument("--html")
    parser.add_argument("--preview")
    args = parser.parse_args()
    try:
        artifact = compute(snapshot_from(load(args.record)))
        write(args.artifact, canonical(artifact) + u"\n")
        if args.html:
            write(args.html, render_html(artifact))
        if args.preview:
            write(args.preview, canonical(artifact["preview"]) + u"\n")
    except (IOError, ValueError, KeyError, TypeError, AttributeError) as error:
        print("Compound legend stopped: %s" % error, file=sys.stderr)
        return 2
    return 1 if artifact["preview"]["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
