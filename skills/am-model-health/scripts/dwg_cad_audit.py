"""AutoCAD & DWG / DXF Audit Engine (Issue #188 Phase 1).

Audits CAD drawings for:
1. Layer naming standards (CAD-LAY-01) - AIA layer naming conventions.
2. Xref inspector (CAD-XREF-01) - missing paths, absolute network drive letters, circular links.
3. Block and attribute inventory (CAD-BLK-01) - anonymous blocks, empty attributes.
4. Scale and units verification (CAD-UNIT-01) - $INSUNITS and $MEASUREMENT alignment.

Exports findings directly to am-issue-board format and summary reports.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from collections import defaultdict
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional, Set, Tuple


STANDARD_AIA_DISCIPLINES = ("A", "S", "M", "E", "P", "C", "L", "G", "Q", "T")
STANDARD_AIA_MAJOR_GROUPS = (
    "WALL", "DOOR", "GLAZ", "FLOR", "ROOF", "CLNG", "COLS", "FURN",
    "EQPM", "ANNO", "DIMS", "TEXT", "GRID", "LEVL", "PATT", "DETL",
    "HVAC", "DUCT", "PIPE", "PLUM", "SANR", "LITE", "POWR", "FIRE",
)

INSUNITS_MAP = {
    0: "Unspecified",
    1: "Inches",
    2: "Feet",
    3: "Miles",
    4: "Millimeters",
    5: "Centimeters",
    6: "Meters",
    7: "Kilometers",
}


def check_layer_standards(layers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Verify layer naming compliance against CAD standards (CAD-LAY-01)."""
    findings: List[Dict[str, Any]] = []

    for lay in layers:
        name = str(lay.get("name", "")).strip()
        if not name:
            continue

        # Special CAD layers
        if name.upper() in ("0", "DEFPOINTS"):
            if lay.get("entityCount", 0) > 0:
                findings.append({
                    "ruleId": "CAD-LAY-01",
                    "severity": "warning",
                    "category": "CAD Layer",
                    "target": name,
                    "message": f"Layer '{name}' contains {lay.get('entityCount')} entities. Best practice is to draw on dedicated discipline layers.",
                })
            continue

        # Check AIA format: D-MAJOR[-MINOR] e.g. A-WALL, A-DOOR-FULL
        parts = name.split("-")
        discipline = parts[0].upper()
        if len(discipline) != 1 or discipline not in STANDARD_AIA_DISCIPLINES:
            findings.append({
                "ruleId": "CAD-LAY-01",
                "severity": "warning",
                "category": "CAD Layer",
                "target": name,
                "message": f"Layer '{name}' does not start with standard single-letter discipline code ({', '.join(STANDARD_AIA_DISCIPLINES)}).",
            })
            continue

        if len(parts) > 1:
            major = parts[1].upper()
            if major not in STANDARD_AIA_MAJOR_GROUPS and not re.match(r"^[A-Z0-9]{3,4}$", major):
                findings.append({
                    "ruleId": "CAD-LAY-01",
                    "severity": "info",
                    "category": "CAD Layer",
                    "target": name,
                    "message": f"Layer '{name}' uses non-standard major group '{major}'.",
                })

    return findings


def read_cad_policy(path):
    from cad_file_inventory import local_path, snapshot_file, validate_json

    source = local_path(path)
    source = source.parent.resolve(strict=True) / source.name
    with TemporaryDirectory(prefix="archi-cad-policy-") as temporary:
        snapshot = Path(temporary) / "policy.json"
        digest = snapshot_file(source, snapshot, limit=256 * 1024)
        return digest, validate_json(snapshot.read_bytes())


def read_layer_policy(path):
    digest, policy = read_cad_policy(path)
    if (not isinstance(policy, dict) or set(policy) != {"layers"} or not isinstance(policy["layers"], dict)
            or not 1 <= len(policy["layers"]) <= 512):
        raise ValueError("Supply a layer policy with one to 512 named layers.")
    rules = {}
    for name, rule in policy["layers"].items():
        if not name.strip() or name.strip() != name or len(name) > 255 or name.casefold() in rules:
            raise ValueError("Use distinct, exact layer names.")
        if not isinstance(rule, dict) or not rule or set(rule) - {"aci", "rgb", "linetype"}:
            raise ValueError("Use ACI, RGB, or linetype checks for each layer.")
        if ("aci" in rule and (type(rule["aci"]) is not int or not 1 <= rule["aci"] <= 255 or "rgb" in rule)
                or "rgb" in rule and (not isinstance(rule["rgb"], str)
                                      or re.fullmatch(r"#[0-9a-fA-F]{6}", rule["rgb"]) is None)
                or "linetype" in rule and (not isinstance(rule["linetype"], str)
                                           or not 1 <= len(rule["linetype"].strip()) <= 255)):
            raise ValueError("Use one color mode and a valid linetype.")
        rules[name.casefold()] = (name, rule)
    return digest, rules


def check_layer_policy(drawing, rules):
    layers = {}
    for layer in drawing["layers"]:
        layers.setdefault(layer["name"].casefold(), []).append(layer)
    findings, unchecked, unmatched = [], [], []
    checked = 0
    for key, (name, rule) in rules.items():
        matching = layers.get(key, [])
        if not matching:
            unmatched.append(name)
            continue
        if len(matching) != 1:
            unchecked.extend({"layer": name, "field": field} for field in rule)
            continue

        layer = matching[0]
        aci = layer.get("color")
        rgb = layer.get("true_color")
        color = None
        if type(rgb) is int and 0 <= rgb <= 0xFFFFFF:
            color = ("rgb", f"#{rgb:06x}")
        elif rgb is None and type(aci) is int and 1 <= abs(aci) <= 255:
            color = ("aci", abs(aci))
        for field, expected in rule.items():
            observed = field
            if field == "linetype":
                actual = layer.get("linetype")
                if not isinstance(actual, str) or not actual.strip():
                    unchecked.append({"layer": layer["name"], "field": field})
                    continue
                equal = actual.casefold() == expected.strip().casefold()
            elif color is None:
                unchecked.append({"layer": layer["name"], "field": field})
                continue
            else:
                observed, actual = color
                equal = observed == field and actual == (expected.lower() if field == "rgb" else expected)

            checked += 1
            if not equal:
                findings.append({"ruleId": "CAD-LAY-02", "severity": "warning", "category": "CAD Layer",
                                 "target": layer["name"], "field": field, "expected": expected,
                                 "observed_field": observed, "actual": actual,
                                 "message": f"Layer '{layer['name']}' has {observed} {actual!r}, expected {field} {expected!r}."})
    return findings, {"checked_fields": checked, "unchecked_fields": unchecked, "unmatched_layers": unmatched}


def check_xrefs(xrefs: List[Dict[str, Any]], drawing_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Audit external reference paths and circular links (CAD-XREF-01)."""
    findings: List[Dict[str, Any]] = []
    seen_names: Set[str] = set()

    for xref in xrefs:
        name = str(xref.get("name", "")).strip()
        path = str(xref.get("path", "")).strip()
        status = str(xref.get("status", "loaded")).lower()

        # Check unresolvable / missing
        if status in ("unresolved", "not found", "missing"):
            findings.append({
                "ruleId": "CAD-XREF-01",
                "severity": "error",
                "category": "CAD Xref",
                "target": name,
                "message": f"Xref '{name}' is unresolved or missing at path: '{path}'.",
            })

        # Check hardcoded absolute drive letter (e.g. C:\work\ or Z:\projects\)
        if re.match(r"^[A-Za-z]:[\\/]", path):
            findings.append({
                "ruleId": "CAD-XREF-01",
                "severity": "warning",
                "category": "CAD Xref",
                "target": name,
                "message": f"Xref '{name}' uses hardcoded drive letter path '{path}'. Relative or cloud-synced paths recommended.",
            })

        # Check circular xref
        if xref.get("isCircular"):
            findings.append({
                "ruleId": "CAD-XREF-01",
                "severity": "error",
                "category": "CAD Xref",
                "target": name,
                "message": f"Circular xref dependency detected on '{name}'.",
            })

    return findings


def check_blocks_and_attributes(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Inspect block references and attributes (CAD-BLK-01)."""
    findings: List[Dict[str, Any]] = []

    for blk in blocks:
        name = str(blk.get("name", "")).strip()
        count = blk.get("count", 0)

        # Report anonymous blocks as inventory.
        if name.startswith("*U") or name.startswith("*D"):
            findings.append({
                "ruleId": "CAD-BLK-01",
                "severity": "info",
                "category": "CAD Block",
                "target": name,
                "message": f"Anonymous block '{name}' has {count} recorded inserts.",
            })

        # Empty attributes
        for attr in blk.get("attributes", []):
            if attr.get("isRequired") and not str(attr.get("value", "")).strip():
                findings.append({
                    "ruleId": "CAD-BLK-01",
                    "severity": "warning",
                    "category": "CAD Block Attribute",
                    "target": f"{name}:{attr.get('tag')}",
                    "message": f"Block '{name}' attribute '{attr.get('tag')}' is blank.",
                })

    return findings


def read_attribute_policy(path):
    digest, policy = read_cad_policy(path)
    if (not isinstance(policy, dict) or set(policy) != {"blocks"} or not isinstance(policy["blocks"], dict)
            or not 1 <= len(policy["blocks"]) <= 512):
        raise ValueError("Supply an attribute policy with one to 512 named blocks.")
    rules = {}
    for name, tags in policy["blocks"].items():
        if not name.strip() or name != name.strip() or len(name) > 255 or name.casefold() in rules:
            raise ValueError("Use distinct, exact block names.")
        if (not isinstance(tags, list) or not 1 <= len(tags) <= 64
                or any(not isinstance(tag, str) or not tag.strip() or tag != tag.strip() or len(tag) > 255 for tag in tags)
                or len({tag.casefold() for tag in tags}) != len(tags)):
            raise ValueError("Use one to 64 distinct attribute tags per block.")
        rules[name.casefold()] = (name, tags)
    return digest, rules


def attribute_state(attached, definitions, incomplete_links):
    if len(attached) > 1:
        return "duplicate"
    if attached:
        value = attached[0]["value"]
    elif incomplete_links or len(definitions) > 1 or any(row["constant"] is None for row in definitions):
        return "unchecked"
    elif definitions and definitions[0]["constant"]:
        value = definitions[0]["value"]
    else:
        return "missing"
    if value is None:
        return "unchecked"
    return "filled" if value.strip() else "blank"


def check_attribute_policy(drawing, rules):
    by_name = defaultdict(list)
    for block in drawing["blocks"]:
        by_name[block["name"].casefold()].append(block)
    uncertain = bool(drawing["unassociated_attributes"] or drawing["unassociated_definitions"])
    findings, unchecked, unmatched = [], [], []
    checked = total = 0
    for key, (name, tags) in rules.items():
        blocks = by_name.get(key, [])
        if len(blocks) > 1:
            unchecked.append({"block": name, "reason": "ambiguous_block_definition"})
            continue
        if not blocks or not blocks[0]["insert_handles"]:
            unmatched.append(name)
            continue
        block = blocks[0]
        total += len(block["insert_handles"]) * len(tags)
        if total > 100000:
            raise ValueError("The attribute policy exceeds 100,000 insert-tag checks.")
        attached, definitions = defaultdict(list), defaultdict(list)
        for attribute in block["attributes"]:
            attached[attribute["insert_handle"], attribute["tag"].casefold()].append(attribute)
        for definition in block["definitions"]:
            definitions[definition["tag"].casefold()].append(definition)
        for insert in block["insert_handles"]:
            for tag in tags:
                folded = tag.casefold()
                status = attribute_state(attached.get((insert, folded), ()), definitions.get(folded, ()), uncertain)
                target = {"block": block["name"], "insert_handle": insert, "tag": tag}
                if status == "unchecked":
                    unchecked.append(target)
                    continue
                checked += 1
                if status != "filled":
                    findings.append({"ruleId": "CAD-BLK-02", "severity": "warning", "category": "CAD Block Attribute",
                                     "target": f"{block['name']}:{tag}", "state": status, **target,
                                     "message": f"Block '{block['name']}' insert {insert}: required tag '{tag}' is {status}."})
    return findings, {"checked_fields": checked, "unchecked_fields": unchecked, "unmatched_blocks": unmatched}


def check_scale_and_units(header: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Audit $INSUNITS and $MEASUREMENT settings (CAD-UNIT-01)."""
    findings: List[Dict[str, Any]] = []

    insunits = header.get("$INSUNITS")
    if insunits is not None:
        try:
            val = int(insunits)
            unit_name = INSUNITS_MAP.get(val, "Unknown")
            if val == 0:
                findings.append({
                    "ruleId": "CAD-UNIT-01",
                    "severity": "error",
                    "category": "CAD Units",
                    "target": "$INSUNITS",
                    "message": "$INSUNITS is 0 (Unspecified). Revit import will guess units and may scale incorrectly.",
                })
        except (ValueError, TypeError):
            pass

    measurement = header.get("$MEASUREMENT")
    if measurement is not None and insunits is not None:
        try:
            m_val = int(measurement)  # 0 = English (Imperial), 1 = Metric
            u_val = int(insunits)
            # Imperial measurement with metric insunits or vice versa
            is_metric_unit = u_val in (4, 5, 6, 7)
            is_imperial_unit = u_val in (1, 2, 3)
            if m_val == 0 and is_metric_unit:
                findings.append({
                    "ruleId": "CAD-UNIT-01",
                    "severity": "warning",
                    "category": "CAD Units",
                    "target": "$MEASUREMENT",
                    "message": f"$MEASUREMENT is English (0), but $INSUNITS is Metric ({INSUNITS_MAP.get(u_val)}).",
                })
            elif m_val == 1 and is_imperial_unit:
                findings.append({
                    "ruleId": "CAD-UNIT-01",
                    "severity": "warning",
                    "category": "CAD Units",
                    "target": "$MEASUREMENT",
                    "message": f"$MEASUREMENT is Metric (1), but $INSUNITS is Imperial ({INSUNITS_MAP.get(u_val)}).",
                })
        except (ValueError, TypeError):
            pass

    return findings


def run_cad_audit(drawing_data: Dict[str, Any], policy_findings=()) -> Dict[str, Any]:
    """Execute all CAD audit rules on drawing data dictionary."""
    all_findings: List[Dict[str, Any]] = []
    finding_id = 1

    layers = drawing_data.get("layers", [])
    xrefs = drawing_data.get("xrefs", [])
    blocks = drawing_data.get("blocks", [])
    header = drawing_data.get("header", {})

    for f in check_layer_standards(layers):
        f["id"] = f"CAD-{finding_id:04d}"
        finding_id += 1
        all_findings.append(f)

    for f in check_xrefs(xrefs):
        f["id"] = f"CAD-{finding_id:04d}"
        finding_id += 1
        all_findings.append(f)

    for f in [*check_blocks_and_attributes(blocks), *policy_findings]:
        f["id"] = f"CAD-{finding_id:04d}"
        finding_id += 1
        all_findings.append(f)

    for f in check_scale_and_units(header):
        f["id"] = f"CAD-{finding_id:04d}"
        finding_id += 1
        all_findings.append(f)

    errors = sum(1 for f in all_findings if f["severity"] == "error")
    warnings = sum(1 for f in all_findings if f["severity"] == "warning")
    infos = sum(1 for f in all_findings if f["severity"] == "info")

    return {
        "drawing_name": drawing_data.get("filename", "unknown.dwg"),
        "total_layers": len(layers),
        "total_xrefs": len(xrefs),
        "total_findings": len(all_findings),
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "infos": infos,
        },
        "findings": all_findings,
    }



def audit_file(args):
    from cad_file_inventory import FILE_LIMIT, inspect_drawing, local_path

    try:
        drawing = inspect_drawing(args.input, args.dwgread)
        policy_findings = []
        for option, coverage, reader, check, label in (
                ("layer_policy", "layer_style_policy", read_layer_policy, check_layer_policy, "layer style"),
                ("attribute_policy", "required_attribute_policy", read_attribute_policy, check_attribute_policy, "attribute")):
            path = getattr(args, option, None)
            if not path:
                continue
            digest, rules = reader(path)
            findings, evidence = check(drawing, rules)
            policy_findings.extend(findings)
            drawing[option] = {"sha256": digest, **evidence}
            complete = evidence["checked_fields"] > 0 and not evidence["unchecked_fields"]
            drawing["coverage"][coverage] = "checked" if complete else "incomplete"
            if not complete:
                drawing["notes"].append(f"One or more requested {label} checks lack drawing evidence.")
        result = run_cad_audit(drawing, policy_findings)
        result.update(status="incomplete" if drawing["notes"] else "audited",
                      inventory=drawing)
        report = result if args.output or not args.summary else {
            key: result[key] for key in ("drawing_name", "status", "summary")
        } | {"notes": drawing["notes"], "coverage": drawing["coverage"]}
        encoded = (json.dumps(report, ensure_ascii=True, indent=2) + "\n").encode()
        if len(encoded) > FILE_LIMIT:
            raise ValueError("Audit report exceeds the supported size.")
        if args.output:
            with local_path(args.output).open("xb") as destination:
                destination.write(encoded)
        else:
            sys.stdout.write(encoded.decode("ascii"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
        print(f"CAD audit failed: {error}", file=sys.stderr)
        return 2
    if drawing["notes"]:
        return 3
    return int(result["summary"]["errors"] > 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoCAD / DWG & DXF Audit Tool (Issue #188 Phase 1)")
    parser.add_argument("input", help="Path to a local DWG, DXF or JSON drawing inventory")
    parser.add_argument("--output", help="Path to write issue findings JSON")
    parser.add_argument("--summary", action="store_true", help="Print summary only")

    parser.add_argument("--dwgread", help="Absolute path to trusted LibreDWG 0.14 dwgread")
    parser.add_argument("--layer-policy", help="Local JSON layer color and linetype standard")
    parser.add_argument("--attribute-policy", help="Local JSON standard for required block attribute tags")
    args = parser.parse_args()

    if Path(args.input).suffix.lower() in {".dxf", ".dwg"}:
        return audit_file(args)
    if args.layer_policy or args.attribute_policy:
        parser.error("Policy options require a direct DWG or DXF drawing.")

    if not os.path.isfile(args.input):
        print(f"Error: input file '{args.input}' not found.", file=sys.stderr)
        return 1

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    result = run_cad_audit(data)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote {result['total_findings']} findings to {args.output}")

    summary = result["summary"]
    print(f"CAD Audit: {result['drawing_name']}")
    print(f"Layers: {result['total_layers']} | Xrefs: {result['total_xrefs']}")
    print(f"Findings: {summary['errors']} errors, {summary['warnings']} warnings, {summary['infos']} infos.")

    if not args.summary and result["findings"]:
        print("\nFindings detail:")
        for finding in result["findings"][:10]:
            print(f"  [{finding['severity'].upper()}] {finding['ruleId']}: {finding['message']}")
        if len(result["findings"]) > 10:
            print(f"  ... and {len(result['findings']) - 10} more findings")

    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
