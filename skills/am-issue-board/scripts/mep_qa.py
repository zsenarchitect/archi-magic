"""MEP QA audit engine (Issue #186 Phase 1).

Audits MEP elements for:
1. System classification and naming (MEP-SYS-01)
2. Unconnected ends and open connectors (MEP-CON-01)
3. Missing dimensions, sizing, or flow data (MEP-FLOW-01)
4. Equipment maintenance clearance zones (MEP-CLR-01)

Exports findings directly to am-issue-board format.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Any, Dict, List


VALID_SYSTEM_PREFIXES = (
    "SA", "RA", "EA", "OA", "FA",     # HVAC air
    "CW", "HW", "CHW", "HHW", "SAN",  # Piping
    "VENT", "STORM", "FIRE", "GAS",   # Plumbing & Fire
)


def check_mep_system(element: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check system assignment and naming (MEP-SYS-01)."""
    findings: List[Dict[str, Any]] = []
    category = str(element.get("category", ""))
    params = element.get("parameters", {})

    # Only check categories that belong to MEP distribution systems
    mep_cats = ("Ducts", "Pipes", "Duct Fittings", "Pipe Fittings", "Mechanical Equipment", "Air Terminals")
    if not any(c in category for c in mep_cats):
        return findings

    sys_type = params.get("System Type") or params.get("MEPSystem") or params.get("System Classification")
    sys_name = params.get("System Name")

    if not sys_type or str(sys_type).strip() in ("", "Undefined", "None"):
        findings.append({
            "ruleId": "MEP-SYS-01",
            "severity": "error",
            "elementId": element.get("id"),
            "category": category,
            "level": element.get("level", "Unassigned"),
            "message": f"{category} (ID {element.get('id')}) has no assigned MEP system type.",
            "location": element.get("location"),
        })
    elif sys_name:
        s_name = str(sys_name).strip()
        prefix = s_name.split("-")[0].strip().upper() if "-" in s_name else s_name.split(" ")[0].strip().upper()
        if prefix not in VALID_SYSTEM_PREFIXES:
            findings.append({
                "ruleId": "MEP-SYS-01",
                "severity": "warning",
                "elementId": element.get("id"),
                "category": category,
                "level": element.get("level", "Unassigned"),
                "message": f"System name '{s_name}' does not start with standard prefix ({', '.join(VALID_SYSTEM_PREFIXES[:6])}...).",
                "location": element.get("location"),
            })

    return findings


DISTRIBUTION_CATEGORIES = {
    "Ducts", "Flex Ducts", "Duct Fittings", "Pipes", "Flex Pipes", "Pipe Fittings",
    "Conduits", "Conduit Fittings", "Cable Trays", "Cable Tray Fittings",
}


def check_open_connectors(element: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Distinguish confirmed open ends from incomplete connector evidence."""
    findings: List[Dict[str, Any]] = []
    connectors = element.get("connectors", [])
    category = str(element.get("category", ""))
    base = {
        "elementId": element.get("id"),
        "category": category,
        "level": element.get("level", "Unassigned"),
        "location": element.get("location"),
    }
    invalid_inventory = not isinstance(connectors, list)
    if invalid_inventory:
        connectors = []
    incomplete = invalid_inventory or (not connectors and category in DISTRIBUTION_CATEGORIES)

    for idx, connector in enumerate(connectors):
        connected = connector.get("isConnected") if isinstance(connector, dict) else None
        if type(connected) is not bool:
            incomplete = True
        elif not connected:
            domain = connector.get("domain", "Unknown")
            findings.append({
                **base,
                "ruleId": "MEP-CON-01",
                "severity": "error" if "Fitting" in category or "Duct" in category or "Pipe" in category else "warning",
                "message": f"Open connector #{idx} ({domain}) on {category} ID {base['elementId']}.",
                "location": connector.get("origin") or base["location"],
            })

    if incomplete:
        findings.append({
            **base,
            "ruleId": "MEP-CON-02",
            "severity": "warning",
            "message": f"Connector data is incomplete for {category} ID {base['elementId']}. "
                       "Check the connector inventory and connection states in Revit.",
        })
    return findings


AIRFLOW = re.compile(
    r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)"
    r"(?:\s*(?:cfm|l/s|m(?:3|³|\^3)/[sh]))?",
    re.IGNORECASE,
)


def read_airflow(value):
    match = AIRFLOW.fullmatch(str(value).strip())
    if match is None:
        return None
    flow = float(match.group(1))
    return flow if math.isfinite(flow) else None


def check_sizing_and_flow(element: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check missing sizing and missing or nonpositive airflow (MEP-FLOW-01)."""
    findings: List[Dict[str, Any]] = []
    category = str(element.get("category", ""))
    params = element.get("parameters", {})

    if "Duct" in category and not ("Fitting" in category):
        width = params.get("Width")
        height = params.get("Height")
        diam = params.get("Diameter")
        if not (diam or (width and height)):
            findings.append({
                "ruleId": "MEP-FLOW-01",
                "severity": "error",
                "elementId": element.get("id"),
                "category": category,
                "level": element.get("level", "Unassigned"),
                "message": f"Duct ID {element.get('id')} is missing cross-section dimensions (Width/Height or Diameter).",
                "location": element.get("location"),
            })

    if "Pipe" in category and not ("Fitting" in category):
        size = params.get("Size") or params.get("Diameter")
        if not size:
            findings.append({
                "ruleId": "MEP-FLOW-01",
                "severity": "error",
                "elementId": element.get("id"),
                "category": category,
                "level": element.get("level", "Unassigned"),
                "message": f"Pipe ID {element.get('id')} has no dimension or diameter specified.",
                "location": element.get("location"),
            })

    system_type = str(params.get("System Type") or params.get("System Classification") or "")
    if any(kind in system_type.lower() for kind in ("supply", "return", "exhaust")):
        raw_flow = params.get("Flow")
        flow = read_airflow(raw_flow)
        message = None
        if flow is None and category in ("Ducts", "Flex Ducts"):
            message = "has no readable calculated flow"
        elif flow is not None and flow <= 0:
            message = f"has zero or negative calculated flow ({raw_flow})"
        if message is not None:
            findings.append({
                "ruleId": "MEP-FLOW-01",
                "severity": "warning",
                "elementId": element.get("id"),
                "category": category,
                "level": element.get("level", "Unassigned"),
                "message": f"{category} ID {element.get('id')} {message}.",
                "location": element.get("location"),
            })

    return findings


def check_clearance_zones(element: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check equipment maintenance clearance zones (MEP-CLR-01)."""
    findings: List[Dict[str, Any]] = []
    category = str(element.get("category", ""))
    params = element.get("parameters", {})

    if "Mechanical Equipment" in category:
        has_clearance = (
            params.get("Clearance Zone")
            or params.get("Service Clearance")
            or params.get("Maintenance Clearance")
            or element.get("hasClearanceBox")
        )
        if not has_clearance:
            findings.append({
                "ruleId": "MEP-CLR-01",
                "severity": "warning",
                "elementId": element.get("id"),
                "category": category,
                "level": element.get("level", "Unassigned"),
                "message": f"Mechanical equipment '{element.get('name', '')}' (ID {element.get('id')}) has no documented service clearance zone.",
                "location": element.get("location"),
            })

    return findings


def run_mep_audit(elements: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Execute all MEP QA audit rules on provided element set."""
    all_findings: List[Dict[str, Any]] = []
    finding_id = 1

    for el in elements:
        for f in check_mep_system(el):
            f["id"] = f"MEP-{finding_id:04d}"
            finding_id += 1
            all_findings.append(f)

        for f in check_open_connectors(el):
            f["id"] = f"MEP-{finding_id:04d}"
            finding_id += 1
            all_findings.append(f)

        for f in check_sizing_and_flow(el):
            f["id"] = f"MEP-{finding_id:04d}"
            finding_id += 1
            all_findings.append(f)

        for f in check_clearance_zones(el):
            f["id"] = f"MEP-{finding_id:04d}"
            finding_id += 1
            all_findings.append(f)

    errors = sum(1 for f in all_findings if f["severity"] == "error")
    warnings = sum(1 for f in all_findings if f["severity"] == "warning")
    infos = sum(1 for f in all_findings if f["severity"] == "info")

    return {
        "total_elements_audited": len(elements),
        "total_findings": len(all_findings),
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "infos": infos,
        },
        "findings": all_findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MEP QA Audit Tool (Issue #186 Phase 1)")
    parser.add_argument("input", help="Path to JSON file containing extracted MEP elements")
    parser.add_argument("--output", help="Path to write issue board findings JSON")
    parser.add_argument("--summary", action="store_true", help="Print summary only")

    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: input file '{args.input}' not found.", file=sys.stderr)
        return 1

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    elements = data if isinstance(data, list) else data.get("elements", [])
    result = run_mep_audit(elements)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"Wrote {result['total_findings']} findings to {args.output}")

    summary = result["summary"]
    print(f"MEP QA Audit: {result['total_elements_audited']} elements audited.")
    print(f"Results: {summary['errors']} errors, {summary['warnings']} warnings, {summary['infos']} infos.")

    if not args.summary and result["findings"]:
        print("\nFindings detail:")
        for finding in result["findings"][:10]:
            print(f"  [{finding['severity'].upper()}] {finding['ruleId']}: {finding['message']}")
        if len(result["findings"]) > 10:
            print(f"  ... and {len(result['findings']) - 10} more findings")

    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
