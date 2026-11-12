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
from pathlib import Path
import re
import sys
from typing import Any, Dict, List
from uuid import UUID


VALID_SYSTEM_PREFIXES = (
    "SA", "RA", "EA", "OA", "FA",     # HVAC air
    "CW", "HW", "CHW", "HHW", "SAN",  # Piping
    "VENT", "STORM", "FIRE", "GAS",   # Plumbing & Fire
)


def system_prefix(name):
    name = name.strip()
    return name.split("-")[0].strip().upper() if "-" in name else name.split(" ")[0].upper()


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
        prefix = system_prefix(s_name)
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


LENGTH = re.compile(
    r"(?:(?:dn|nps)\s*)?"
    r"(?:(?P<decimal>[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
    r"|(?:(?P<whole>[0-9]+)\s+)?(?P<numerator>[0-9]+)/(?P<denominator>[0-9]+))"
    r"""\s*(?:mm|cm|m|ft|in|["'′″])?""",
    re.IGNORECASE,
)


def positive_dimension(value):
    if type(value) in (int, float):
        return value > 0 and (type(value) is int or math.isfinite(value))
    if not isinstance(value, str) or len(value) > 120:
        return False
    match = LENGTH.fullmatch(value.strip())
    if match is None:
        return False
    if match["decimal"] is not None:
        return float(match["decimal"]) > 0
    return int(match["denominator"]) > 0 and (int(match["whole"] or "0") > 0 or int(match["numerator"]) > 0)


def check_sizing_and_flow(element: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Check sizing evidence and missing or nonpositive airflow (MEP-FLOW-01)."""
    findings: List[Dict[str, Any]] = []
    category = element.get("category", "")
    params = element.get("parameters", {})
    diameter = positive_dimension(params.get("Diameter"))
    size = positive_dimension(params.get("Size"))
    rectangular = all(positive_dimension(params.get(field)) for field in ("Width", "Height"))
    message = None
    if category in ("Ducts", "Flex Ducts") and not (diameter or rectangular):
        message = "needs a positive diameter or positive width and height"
    elif category in ("Pipes", "Flex Pipes", "Conduits") and not (size or diameter):
        message = "needs a readable positive size or diameter"
    elif category == "Cable Trays" and not rectangular:
        message = "needs a positive width and height"
    if message:
        findings.append({
            "ruleId": "MEP-FLOW-01",
            "severity": "error",
            "elementId": element.get("id"),
            "category": category,
            "level": element.get("level", "Unassigned"),
            "message": f"{category} ID {element.get('id')} {message}.",
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

    return {
        "total_elements_audited": len(elements),
        "total_findings": len(all_findings),
        "summary": summarize_findings(all_findings),
        "findings": all_findings,
    }


def summarize_findings(findings):
    return {severity + "s": sum(row["severity"] == severity for row in findings)
            for severity in ("error", "warning", "info")}


def positive_id(value):
    return (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value) is not None
            and int(value) <= 2**63 - 1)


def audit_input(data):
    elements = data.get("elements") if isinstance(data, dict) else data
    if not isinstance(elements, list) or not elements:
        raise ValueError("The audit needs a nonempty extracted MEP element list.")
    seen = set()
    for row in elements:
        if (not isinstance(row, dict) or not isinstance(row.get("category"), str) or not row["category"].strip()
                or not isinstance(row.get("parameters", {}), dict)):
            raise ValueError("Each element needs a category and a valid parameter map.")
        value = row.get("id")
        identifier = str(value) if type(value) is int else value
        if not positive_id(identifier) or identifier in seen:
            raise ValueError("Each element needs a distinct positive Revit ID.")
        seen.add(identifier)
    return elements


SYSTEM_CATEGORIES = {
    "Ducts", "Flex Ducts", "Duct Fittings", "Air Terminals",
    "Pipes", "Flex Pipes", "Pipe Fittings", "Mechanical Equipment",
}
SIZE_CATEGORIES = {"Ducts", "Flex Ducts", "Pipes", "Flex Pipes", "Conduits", "Cable Trays"}
DUCT_CATEGORIES = {"Ducts", "Flex Ducts"}
NATIVE_CHECKS = ("system", "connectivity", "size", "airflow", "clearance")


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def model_identity(identity):
    return (isinstance(identity, dict) and set(identity) == {"project_uid", "path_sha256"}
            and isinstance(identity["project_uid"], str) and 0 < len(identity["project_uid"]) <= 160
            and isinstance(identity["path_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", identity["path_sha256"]) is not None)


def validate_native_source(source):
    if (not isinstance(source, dict)
            or set(source) != {"host_identity", "document_identity", "link_instance_id", "link_instance_uid", "transform"}
            or not model_identity(source["host_identity"]) or not model_identity(source["document_identity"])
            or not isinstance(source["transform"], list) or len(source["transform"]) != 12
            or not all(finite_number(value) for value in source["transform"])):
        raise ValueError("The native MEP snapshot has an invalid Revit source.")
    if source["link_instance_id"] is None:
        if (source["link_instance_uid"] is not None or source["document_identity"] != source["host_identity"]
                or source["transform"] != [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]):
            raise ValueError("The native MEP host identity or transform is inconsistent.")
    elif (not positive_id(source["link_instance_id"]) or not isinstance(source["link_instance_uid"], str)
          or not 0 < len(source["link_instance_uid"]) <= 160):
        raise ValueError("The native MEP snapshot needs an exact link instance identity.")


def evidence_state(evidence, *, allow_inapplicable=False):
    if isinstance(evidence, dict):
        state = evidence.get("state")
        if state == "observed" and set(evidence) == {"state", "value"} and finite_number(evidence["value"]):
            return state
        if set(evidence) == {"state"} and (
            state in ("absent", "unavailable") or (allow_inapplicable and state == "not_applicable")
        ):
            return state
    raise ValueError("The native MEP snapshot contains invalid numeric evidence.")


def native_element_findings(row):
    category = row["category"]
    checks = dict.fromkeys(NATIVE_CHECKS, "not_applicable")
    findings = []

    def finding(rule, severity, message, **details):
        findings.append({"ruleId": rule, "severity": severity, "elementId": row["id"],
                         "category": category, "message": message, **details})

    connectors = row["connectors"]
    connector_evidence = row["connector_evidence"]
    if (not isinstance(connectors, list) or len(connectors) > 32
            or connector_evidence["inventory"] not in ("complete", "unavailable")
            or any(type(connector_evidence[key]) is not int or not 0 <= connector_evidence[key] <= 4000
                   for key in ("unsupported", "assignments_unknown"))
            or connector_evidence["assignments_unknown"] > len(connectors)
            or (connector_evidence["inventory"] == "unavailable" and connectors)):
        raise ValueError("The native MEP connector inventory is invalid.")
    seen = set()
    for connector in connectors:
        connector_id, connected, point = connector["id"], connector["isConnected"], connector["origin"]
        if (type(connector_id) is not int or connector_id < 0 or connector_id in seen
                or connector["type"] not in ("End", "Curve", "Physical")
                or (connected is not None and type(connected) is not bool)
                or (point is not None and (set(point) != set("xyz") or not all(
                    finite_number(value) for value in point.values())))):
            raise ValueError("The native MEP snapshot contains an invalid physical connector.")
        seen.add(connector_id)
        if connected is False:
            severity = "error" if any(word in category for word in ("Duct", "Pipe", "Fitting")) else "warning"
            finding("MEP-CON-01", severity, f"Open physical connector {connector_id} on {category}.",
                    connectorId=connector_id, location=point)
    connector_complete = bool(connectors) and connector_evidence["inventory"] == "complete" and not (
        connector_evidence["unsupported"] or any(item["isConnected"] is None for item in connectors))
    checks["connectivity"] = "evaluated" if connector_complete else "unavailable"

    systems = row["systems"]
    if (not isinstance(systems, list) or len(systems) > len(connectors)
            or any(not positive_id(system["id"]) or any(
                system[key] is not None and (not isinstance(system[key], str) or len(system[key]) > 160)
                for key in ("name", "classification", "calculation")) for system in systems)
            or len({system["id"] for system in systems}) != len(systems)):
        raise ValueError("The native MEP assigned-system inventory is invalid.")
    if category in SYSTEM_CATEGORIES:
        assignment_complete = bool(connectors) and connector_evidence["inventory"] == "complete" and not (
            connector_evidence["unsupported"] or connector_evidence["assignments_unknown"])
        system_complete = assignment_complete and all(
            system["name"] and system["classification"] and not system["classification"].startswith("Undefined")
            for system in systems)
        checks["system"] = "evaluated" if system_complete else "unavailable"
        if assignment_complete and not systems:
            finding("MEP-SYS-01", "error", f"{category} has no assigned MEP system.")
        for system in systems:
            name = system["name"]
            if name and system_prefix(name) not in VALID_SYSTEM_PREFIXES:
                finding("MEP-SYS-01", "warning", f"System name {name!r} has a nonstandard prefix.",
                        systemId=system["id"])

    if category in SIZE_CATEGORIES:
        sizes = row["sizes"]
        if sizes is not None:
            expected_fields = ({"Diameter"}, {"Width", "Height"}) if category in DUCT_CATEGORIES else (
                ({"Width", "Height"},) if category == "Cable Trays" else ({"Diameter"},))
            if not isinstance(sizes, dict) or set(sizes) not in expected_fields:
                raise ValueError("The native MEP dimensions do not match the audited category.")
            states = {field: evidence_state(value) for field, value in sizes.items()}
            for field, state in states.items():
                if state == "absent" or (state == "observed" and sizes[field]["value"] <= 0):
                    finding("MEP-FLOW-01", "error", f"{category} needs a positive {field.lower()}.")
        else:
            states = {"shape": "unavailable"}
        checks["size"] = "unavailable" if "unavailable" in states.values() else "evaluated"

    if category in DUCT_CATEGORIES:
        airflow = row["airflow"]
        state = evidence_state(airflow, allow_inapplicable=True)
        checks["airflow"] = "evaluated" if state in ("observed", "absent") else state
        if state == "absent":
            finding("MEP-FLOW-01", "warning", f"{category} has no calculated airflow.")
        elif state == "observed" and airflow["value"] <= 0:
            finding("MEP-FLOW-01", "warning", f"{category} has zero or negative calculated airflow.")

    if category == "Mechanical Equipment":
        state = row["clearance"]
        if state not in ("documented", "absent", "unavailable"):
            raise ValueError("The native MEP snapshot contains invalid clearance evidence.")
        checks["clearance"] = "unavailable" if state == "unavailable" else "evaluated"
        if state == "absent":
            finding("MEP-CLR-01", "warning", "Mechanical equipment has no documented service clearance.")
    return findings, checks


def run_native_audit(data):
    if (set(data) != {"kind", "version", "snapshot"} or data["kind"] != "mep-native-snapshot"
            or type(data["version"]) is not int or data["version"] != 1):
        raise ValueError("Unsupported or mixed native MEP snapshot format.")
    snapshot = data["snapshot"]
    if snapshot["status"] != "complete" or not re.fullmatch(r"20[0-9]{2}", snapshot["revit_year"]):
        raise ValueError("The native MEP scope was incomplete or its Revit version was missing.")
    validate_native_source(snapshot["source"])
    clearance = snapshot["clearance"]
    if clearance is not None and (set(clearance) != {"guid", "absent_value"}
                                  or str(UUID(clearance["guid"])) != clearance["guid"]
                                  or type(clearance["absent_value"]) is not int
                                  or clearance["absent_value"] not in (0, 1)):
        raise ValueError("The native MEP clearance mapping is invalid.")
    rows = snapshot["elements"]
    inventory = snapshot["coverage"]
    if (not isinstance(rows, list) or len(rows) > 500
            or set(inventory) != {"elements", "connectors_visited", "connectors_retained"}
            or any(type(value) is not int or value < 0 for value in inventory.values())
            or len(rows) != inventory["elements"]
            or not 0 <= inventory["connectors_retained"] <= inventory["connectors_visited"] <= 4000
            or inventory["connectors_retained"] > 2000):
        raise ValueError("The native MEP snapshot does not have complete inventory counts.")
    seen_ids, seen_uids = set(), set()
    findings, targets, diagnostics = [], {}, []
    coverage = {key: dict.fromkeys(("evaluated", "unavailable", "not_applicable"), 0) for key in NATIVE_CHECKS}
    for row in rows:
        if (not positive_id(row["id"]) or row["id"] in seen_ids
                or not isinstance(row["uid"], str) or not 0 < len(row["uid"]) <= 160
                or row["uid"] in seen_uids
                or row["category"] not in SYSTEM_CATEGORIES | DISTRIBUTION_CATEGORIES
                or (clearance is None and row["category"] == "Mechanical Equipment"
                    and row["clearance"] != "unavailable")):
            raise ValueError("The native MEP snapshot contains an invalid element identity or category.")
        seen_ids.add(row["id"])
        seen_uids.add(row["uid"])
        violations, checks = native_element_findings(row)
        for check, state in checks.items():
            coverage[check][state] += 1
        unavailable = [check for check, state in checks.items() if state == "unavailable"]
        if unavailable:
            diagnostics.append({"elementId": row["id"], "unavailable": unavailable})
        if violations:
            targets[row["id"]] = row
            findings.extend(violations)
    retained = sum(len(row["connectors"]) for row in rows)
    unsupported = sum(row["connector_evidence"]["unsupported"] for row in rows)
    if (inventory["connectors_retained"] != retained or inventory["connectors_visited"] < retained + unsupported
            or (not rows and inventory["connectors_visited"])):
        raise ValueError("The native MEP snapshot connector counts do not agree.")
    for index, row in enumerate(findings, 1):
        row["id"] = f"MEP-{index:04d}"
    return {
        "status": "incomplete_evaluation" if diagnostics else ("audited" if rows else "empty_scope"),
        "source": snapshot["source"], "revit_year": snapshot["revit_year"], "clearance": clearance,
        "inventory": inventory, "coverage": coverage, "diagnostics": diagnostics, "targets": targets,
        "total_elements_audited": len(rows), "total_findings": len(findings),
        "summary": summarize_findings(findings), "findings": findings,
    }


def read_runtime_snapshot(path):
    """Load a complete native query or all retained result pages from one query."""
    limit = 512 * 1024
    with open(path, "rb") as stream:
        encoded = stream.read(8 * limit + 1)
    if len(encoded) > 8 * limit:
        raise ValueError("The saved MEP query exceeds its size limit.")
    saved = json.loads(encoded)
    pages = saved if isinstance(saved, list) else [saved]
    if not 1 <= len(pages) <= 64:
        raise ValueError("Supply up to 64 result pages from one MEP query.")
    for page in pages:
        response = page.get("response", page)
        if (response.get("mode") != "query"
                or response.get("decision") not in ("rolled_back", "no_changes")
                or type(response.get("exit_code")) is not int or response["exit_code"] != 0
                or not isinstance(response.get("changes"), dict)
                or type(response.get("result_truncated")) is not bool
                or not re.fullmatch(r"[0-9a-f]{12}", response.get("run_id", ""))
                or (response.get("result_truncated") is True and response.get("result") is not None)):
            raise ValueError("Supply the full saved responses for one successful pyRevit query.")
        if response.get("result_truncated") is True:
            total = response.get("result_page", {}).get("total_length")
            if type(total) is not int or not 0 < total <= 2 * limit:
                raise ValueError("The retained MEP query exceeds its result size limit.")

    # Reuse the project page's reader for pyRevit envelopes and UTF-16 page offsets.
    viewer_scripts = Path(__file__).resolve().parents[2] / "am-bim-viewer" / "scripts"
    sys.path.insert(0, str(viewer_scripts))
    try:
        from project_page import read_saved_result
    except ImportError as error:
        raise ValueError("The Archi Magic BIM viewer scripts are required for runtime-record input.") from error
    finally:
        sys.path.pop(0)
    result = read_saved_result(saved, ("query",), "The saved MEP query is incomplete or its pages disagree.")
    payload = result["snapshot_json"] if result.get("status") == "ok" else None
    if not isinstance(payload, str) or len(payload) > limit or not payload.isascii():
        raise ValueError("The query did not return a complete bounded native MEP snapshot.")
    return json.loads(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="MEP QA Audit Tool (Issue #186 Phase 1)")
    parser.add_argument("input", help="Path to JSON file containing extracted MEP elements")
    parser.add_argument("--runtime-record", action="store_true", help="Read a full saved pyRevit query or its pages")
    parser.add_argument("--output", help="Path to write issue board findings JSON")
    parser.add_argument("--summary", action="store_true", help="Print summary only")

    args = parser.parse_args()

    try:
        if args.runtime_record:
            data, native = read_runtime_snapshot(args.input), True
        else:
            with open(args.input, "r", encoding="utf-8") as stream:
                data = json.load(stream)
                native = isinstance(data, dict) and (
                    "snapshot" in data or str(data.get("kind", "")).startswith("mep-native-"))
                if native and stream.tell() > 512 * 1024:
                    raise ValueError("The native MEP snapshot exceeds 512 KiB.")
        result = run_native_audit(data) if native else run_mep_audit(audit_input(data))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
        print(f"MEP audit could not read the input. {error}", file=sys.stderr)
        return 2

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            payload = {"kind": "mep-native-report", "version": 1, "report": result} if native else result
            json.dump(payload, f, indent=2)
        print(f"Wrote {result['total_findings']} findings to {args.output}")

    summary = result["summary"]
    print(f"MEP QA Audit: {result['total_elements_audited']} elements audited.")
    print(f"Results: {summary['errors']} errors, {summary['warnings']} warnings, {summary['infos']} infos.")
    if native:
        print(f"Native evaluation: {result['status']}.")

    if not args.summary and result["findings"]:
        print("\nFindings detail:")
        for finding in result["findings"][:10]:
            print(f"  [{finding['severity'].upper()}] {finding['ruleId']}: {finding['message']}")
        if len(result["findings"]) > 10:
            print(f"  ... and {len(result['findings']) - 10} more findings")

    if native and result["status"] == "incomplete_evaluation":
        return 3
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
