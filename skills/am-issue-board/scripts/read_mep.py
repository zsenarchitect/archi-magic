# -*- coding: utf-8 -*-
"""Extract bounded MEP evidence using pyRevit run_query."""

import json
import math
import re

from System import Guid, Int64
from System.Collections.Generic import List
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

import issue_board as ib


ELEMENT_LIMIT = 500
CONNECTOR_LIMIT = 2000
CONNECTOR_VISIT_LIMIT = 4000
ELEMENT_CONNECTOR_LIMIT = 32
SNAPSHOT_LIMIT = 512 * 1024
ENVELOPE_LIMIT = 1024 * 1024
TEXT = (str, type(u""))
CATEGORIES = (
    ("OST_DuctCurves", "Ducts"), ("OST_FlexDuctCurves", "Flex Ducts"),
    ("OST_DuctFitting", "Duct Fittings"), ("OST_DuctTerminal", "Air Terminals"),
    ("OST_PipeCurves", "Pipes"), ("OST_FlexPipeCurves", "Flex Pipes"),
    ("OST_PipeFitting", "Pipe Fittings"), ("OST_MechanicalEquipment", "Mechanical Equipment"),
    ("OST_Conduit", "Conduits"), ("OST_ConduitFitting", "Conduit Fittings"),
    ("OST_CableTray", "Cable Trays"), ("OST_CableTrayFitting", "Cable Tray Fittings"),
)
DUCT_DIAMETER = {"Diameter": ("RBS_CURVE_DIAMETER_PARAM", -1114103, "DuctSize")}
DUCT_WIDTH_HEIGHT = {
    "Width": ("RBS_CURVE_WIDTH_PARAM", -1114101, "DuctSize"),
    "Height": ("RBS_CURVE_HEIGHT_PARAM", -1114102, "DuctSize"),
}
DIMENSIONS = {
    "Pipes": {"Diameter": ("RBS_PIPE_DIAMETER_PARAM", -1140225, "PipeSize")},
    "Conduits": {"Diameter": ("RBS_CONDUIT_DIAMETER_PARAM", -1140123, "ConduitSize")},
    "Cable Trays": {
        "Width": ("RBS_CABLETRAY_WIDTH_PARAM", -1140122, "CableTraySize"),
        "Height": ("RBS_CABLETRAY_HEIGHT_PARAM", -1140121, "CableTraySize"),
    },
}
AIR_CLASSES = ("SupplyAir", "ReturnAir", "ExhaustAir")


class ReadError(ValueError):
    pass


def read_value(getter):
    try:
        return getter()
    except Exception:
        return None


def check_time(clock):
    if clock.ElapsedMilliseconds >= 10000:
        raise ReadError("The MEP query exceeded its ten-second budget.")


def element_id(value):
    if (not isinstance(value, TEXT) or not re.match(r"^[1-9][0-9]{0,18}\Z", value)
            or int(value) > 2**63 - 1):
        raise ReadError("Use positive decimal-string Revit element IDs.")
    return DB.ElementId(Int64(int(value)))


def xyz(point):
    coordinates = [float(point.X), float(point.Y), float(point.Z)]
    if any(math.isinf(value) or math.isnan(value) for value in coordinates):
        raise ReadError("A coordinate could not be read as a finite value.")
    return coordinates


def resolve_source(host, link_id, link_uid):
    if host is None or host.IsFamilyDocument or host.ProjectInformation is None:
        raise ReadError("Open a saved Revit project.")
    host_identity = ib.saved_document_identity(host)
    source = host
    transform = DB.Transform.Identity
    if link_id is not None or link_uid is not None:
        link = host.GetElement(element_id(link_id))
        if not isinstance(link, DB.RevitLinkInstance) or not link_uid or str(link.UniqueId) != link_uid:
            raise ReadError("The reviewed link instance is missing or changed identity.")
        source = link.GetLinkDocument()
        if source is None or source.IsFamilyDocument or source.ProjectInformation is None:
            raise ReadError("The reviewed link is unloaded or is not a project.")
        transform = link.GetTotalTransform()
    identity = {
        "host_identity": host_identity, "document_identity": ib.saved_document_identity(source),
        "link_instance_id": link_id, "link_instance_uid": link_uid,
        "transform": [value for point in (transform.Origin, transform.BasisX, transform.BasisY, transform.BasisZ)
                      for value in xyz(point)],
    }
    return source, transform, identity


def collect_ids(source, requested, categories, clock):
    if requested is not None:
        if not isinstance(requested, list) or not 1 <= len(requested) <= ELEMENT_LIMIT:
            raise ReadError("Supply one to 500 reviewed element IDs.")
        ids = [element_id(value) for value in requested]
        if len(set(requested)) != len(requested):
            raise ReadError("The reviewed element IDs must be distinct.")
        return sorted(ids, key=lambda item: item.Value)
    native_categories = List[DB.BuiltInCategory]()
    for category in categories:
        native_categories.Add(category)
    with DB.FilteredElementCollector(source) as collector:
        iterator = collector.WherePasses(DB.ElementMulticategoryFilter(
            native_categories)).WhereElementIsNotElementType().GetElementIdIterator()
        ids = []
        while iterator.MoveNext():
            check_time(clock)
            if len(ids) == ELEMENT_LIMIT:
                raise ReadError("The category scope exceeds 500 elements. Supply reviewed element IDs.")
            ids.append(iterator.Current)
    check_time(clock)
    return sorted(ids, key=lambda item: item.Value)


def system_record(source, system):
    system_type = read_value(lambda: source.GetElement(system.GetTypeId()))
    name = read_value(lambda: system.Name)
    return {
        "id": str(system.Id.Value),
        "name": name if name and len(name) <= 160 else None,
        "classification": read_value(lambda: str(system_type.SystemClassification)),
        "calculation": read_value(lambda: str(system_type.CalculationLevel)),
    }


def read_connectors(source, element, transform, clock, coverage):
    rows, systems = [], {}
    unsupported, unknown_assignments = 0, 0
    if isinstance(element, DB.MEPCurve):
        connectors = read_value(lambda: element.ConnectorManager.Connectors)
    else:
        connectors = read_value(lambda: element.MEPModel.ConnectorManager.Connectors)
    if connectors is None:
        return rows, [], {"inventory": "unavailable", "unsupported": 0, "assignments_unknown": 0}

    for connector in connectors:
        check_time(clock)
        coverage["connectors_visited"] += 1
        if coverage["connectors_visited"] > CONNECTOR_VISIT_LIMIT:
            raise ReadError("The MEP connector visit limit was reached.")
        kind = read_value(lambda: str(connector.ConnectorType))
        if kind in ("Logical", "Reference"):
            continue
        if kind not in ("End", "Curve", "Physical"):
            unsupported += 1
            continue
        if len(rows) == ELEMENT_CONNECTOR_LIMIT or coverage["connectors_retained"] == CONNECTOR_LIMIT:
            raise ReadError("The physical MEP connector limit was reached.")
        rows.append({
            "id": int(connector.Id), "type": kind,
            "shape": read_value(lambda: str(connector.Shape)),
            "domain": read_value(lambda: str(connector.Domain)),
            "isConnected": read_value(lambda: bool(connector.IsConnected)),
            "origin": read_value(lambda: dict(zip("xyz", xyz(transform.OfPoint(connector.Origin))))),
        })
        coverage["connectors_retained"] += 1
        try:
            system = connector.MEPSystem
        except Exception:
            unknown_assignments += 1
            continue
        if system is not None and system.Id.Value not in systems:
            systems[system.Id.Value] = system_record(source, system)

    rows.sort(key=lambda row: row["id"])
    if len({row["id"] for row in rows}) != len(rows):
        raise ReadError("An element has duplicate physical connector identifiers.")
    return rows, [systems[key] for key in sorted(systems)], {
        "inventory": "complete", "unsupported": unsupported, "assignments_unknown": unknown_assignments,
    }


def numeric_evidence(element, rule):
    name, identifier, spec = rule
    parameter = element.get_Parameter(getattr(DB.BuiltInParameter, name))
    if (parameter is None or parameter.Id.Value != identifier or parameter.StorageType != DB.StorageType.Double
            or not parameter.Definition.GetDataType().Equals(getattr(DB.SpecTypeId, spec))):
        return {"state": "unavailable"}
    if not parameter.HasValue:
        return {"state": "absent"}
    value = float(parameter.AsDouble())
    if math.isnan(value) or math.isinf(value):
        return {"state": "unavailable"}
    return {"state": "observed", "value": value}


def dimension_rules(category, connectors):
    if category in ("Ducts", "Flex Ducts"):
        shapes = {row["shape"] for row in connectors}
        if shapes == {"Round"}:
            return DUCT_DIAMETER
        if shapes in ({"Rectangular"}, {"Oval"}):
            return DUCT_WIDTH_HEIGHT
        return None
    return DIMENSIONS.get("Pipes" if category == "Flex Pipes" else category, {})


def read_clearance(element, config):
    if config is None:
        return "unavailable"
    parameter = element.get_Parameter(config[0])
    if (parameter is None or parameter.StorageType != DB.StorageType.Integer
            or not parameter.Definition.GetDataType().Equals(DB.SpecTypeId.Boolean.YesNo)
            or not parameter.HasValue):
        return "unavailable"
    value = parameter.AsInteger()
    if value not in (0, 1):
        return "unavailable"
    return "absent" if value == config[1] else "documented"


def read_element(source, identifier, categories, transform, clearance, clock, coverage):
    element = source.GetElement(identifier)
    if element is None or isinstance(element, DB.ElementType) or element.Category is None or not element.UniqueId:
        raise ReadError("A selected ID does not identify a supported MEP instance.")
    category = categories.get(element.Category.Id.Value)
    if category is None:
        raise ReadError("A selected element has an unsupported MEP category.")
    connectors, systems, evidence = read_connectors(source, element, transform, clock, coverage)
    rules = dimension_rules(category, connectors)
    sizes = None if rules is None else {
        key: read_value(lambda: numeric_evidence(element, rule)) or {"state": "unavailable"}
        for key, rule in rules.items()
    }
    airflow = None
    if category in ("Ducts", "Flex Ducts"):
        airflow = {"state": "unavailable"}
        if systems and evidence["inventory"] == "complete" and not (
            evidence["unsupported"] or evidence["assignments_unknown"]
        ):
            if all(row["classification"] and row["calculation"] for row in systems):
                airflow = {"state": "not_applicable"}
                if any(row["classification"] in AIR_CLASSES and row["calculation"] in ("All", "Flow")
                       for row in systems):
                    airflow = read_value(lambda: numeric_evidence(
                        element, ("RBS_DUCT_FLOW_PARAM", -1013405, "AirFlow"))) or {"state": "unavailable"}
    return {
        "id": str(identifier.Value), "uid": str(element.UniqueId), "category": category,
        "connectors": connectors, "connector_evidence": evidence, "systems": systems,
        "sizes": sizes, "airflow": airflow,
        "clearance": (read_value(lambda: read_clearance(element, clearance)) or "unavailable")
        if category == "Mechanical Equipment" else None,
    }


def run_query(host, request):
    clock = Stopwatch.StartNew()
    coverage = {"elements": 0, "connectors_visited": 0, "connectors_retained": 0}
    try:
        link_id, link_uid = request.get("link_instance_id"), request.get("link_instance_uid")
        source, transform, identity = resolve_source(host, link_id, link_uid)
        if request.get("describe") is True:
            return {"status": "ok", "source": identity}
        if request.get("source") != identity:
            raise ReadError("The source differs from the reviewed project or link.")
        clearance = None
        if request.get("clearance") is not None:
            config = request["clearance"]
            absence = config["absent_value"]
            if type(absence) is not int or absence not in (0, 1):
                raise ReadError("A clearance check needs an explicit Yes/No absence value.")
            clearance = (Guid(config["guid"]), absence)
        categories = {getattr(DB.BuiltInCategory, name): label for name, label in CATEGORIES}
        ids = collect_ids(source, request.get("element_ids"), categories, clock)
        names = {int(key): label for key, label in categories.items()}
        elements = []
        for identifier in ids:
            check_time(clock)
            elements.append(read_element(source, identifier, names, transform, clearance, clock, coverage))
            coverage["elements"] += 1
        if resolve_source(host, link_id, link_uid)[2] != identity:
            raise ReadError("The project, link instance or placement changed during the MEP query.")
        payload = {"kind": "mep-native-snapshot", "version": 1, "snapshot": {
            "status": "complete", "source": identity, "coverage": coverage,
            "revit_year": str(host.Application.VersionNumber), "elements": elements,
            "clearance": {"guid": str(clearance[0]), "absent_value": clearance[1]} if clearance else None,
        }}
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        result = {"status": "ok", "snapshot_json": encoded}
        if len(encoded) > SNAPSHOT_LIMIT or len(json.dumps(result, ensure_ascii=True)) > ENVELOPE_LIMIT:
            raise ReadError("The MEP snapshot exceeded its encoded result limit.")
        check_time(clock)
        return result
    except ReadError as error:
        return {"status": "incomplete", "message": str(error), "coverage": coverage}
    except Exception:
        return {"status": "incomplete", "message": "Revit could not read the MEP scope.", "coverage": coverage}


if "doc" in globals() and "inputs" in globals():
    result = run_query(doc, inputs)  # noqa: F821
