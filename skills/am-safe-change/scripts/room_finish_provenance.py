# -*- coding: utf-8 -*-
"""Read and reconcile room-finish provenance without changing Revit."""

import hashlib
import json
import math
import re

from Autodesk.Revit.DB.ExtensibleStorage import ContainerType, ExtensibleStorageFilter, Schema
from pyrevit.api import DB
from System import Guid, String


SCHEMA_GUID = "cc22c4e6-6e21-4d64-a509-c6f4ec35725b"
SCHEMA_NAME = "ArchiMagicRoomFinishV1"
FIELDS = {"room_uid", "element_uid", "piece_key", "kind", "type_uid", "level_uid", "observed_sha256"}
MAX_RECORDS = 10000
TEXT = (str, type(u""))


def digest(value):
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def xy(point):
    return [int(math.floor(value * 1e6 + 0.5)) for value in point[:2]]


def edge_key(kind, start, end, midpoint=None):
    points = sorted([xy(start), xy(end)])
    if kind == "Arc" and midpoint is not None:
        points.append(xy(midpoint))
    elif kind != "Line":
        raise ValueError("The finish curve needs separate review.")
    return [kind, points]


def native_xyz(point):
    return [point.X, point.Y, point.Z]


def native_edge(curve):
    kind = curve.GetType().Name
    midpoint = native_xyz(curve.Evaluate(0.5, True)) if kind == "Arc" else None
    return edge_key(kind, native_xyz(curve.GetEndPoint(0)), native_xyz(curve.GetEndPoint(1)), midpoint)


def expected_pieces(row):
    common = {"level_uid": row["level"]["uid"], "phase_uid": row["phase"]["uid"]}
    pieces = {}
    for finish in row["horizontal_finishes"]:
        loops = sorted(sorted(edge_key(edge["curve_type"], edge["start_ft"], edge["end_ft"], edge.get("midpoint_ft"))
                              for edge in loop) for loop in row["boundary_loops"])
        piece = dict(common, kind=finish["kind"], type_uid=finish["type_uid"],
                     offset_ft=float(finish["offset_ft"]) + 0.0, profile=loops)
        pieces[digest(piece)] = piece
    if row.get("wall_finish", {}).get("status") == "reported":
        for loop in row["boundary_loops"]:
            for edge in loop:
                for footprint in edge["wall_finish"]["finish_footprints"]:
                    centerline = edge_key("Line", *footprint["centerline_ft"])
                    piece = dict(common, kind="wall", type_uid=row["wall_finish"]["type_uid"],
                                 centerline=centerline, source_wall_uid=edge["wall_finish"]["wall_uid"])
                    key = digest(piece)
                    if key in pieces:
                        raise ValueError("The preview repeats a finish piece.")
                    pieces[key] = piece
    return pieces


def parameter(element, name, storage, read):
    value = element.get_Parameter(getattr(DB.BuiltInParameter, name))
    if value is None or value.StorageType != storage or not value.HasValue:
        raise ValueError("An existing finish parameter is unavailable.")
    return getattr(value, read)()


def observed_piece(document, element, kind):
    expected_class = {"wall": DB.Wall, "floor": DB.Floor, "ceiling": DB.Ceiling}[kind]
    if not isinstance(element, expected_class) or element.DesignOption is not None:
        raise ValueError("The existing element differs from the finish class or design option.")
    level = document.GetElement(element.LevelId)
    element_type = document.GetElement(element.GetTypeId())
    phase = document.GetElement(element.CreatedPhaseId)
    evidence = {"kind": kind, "level_uid": str(level.UniqueId), "type_uid": str(element_type.UniqueId),
                "phase_uid": str(phase.UniqueId), "demolished_phase_id": str(element.DemolishedPhaseId.Value)}
    if kind == "wall":
        evidence["centerline"] = native_edge(element.Location.Curve)
        evidence["base_offset_ft"] = parameter(element, "WALL_BASE_OFFSET", DB.StorageType.Double, "AsDouble")
        evidence["height_ft"] = parameter(element, "WALL_USER_HEIGHT_PARAM", DB.StorageType.Double, "AsDouble")
        evidence["top_constraint"] = str(parameter(element, "WALL_HEIGHT_TYPE", DB.StorageType.ElementId, "AsElementId").Value)
        evidence["room_bounding"] = parameter(element, "WALL_ATTR_ROOM_BOUNDING", DB.StorageType.Integer, "AsInteger")
        evidence["joined_ends"] = [bool(DB.WallUtils.IsWallJoinAllowedAtEnd(element, end)) for end in (0, 1)]
    else:
        sketch = document.GetElement(element.SketchId)
        if not isinstance(sketch, DB.Sketch) or sketch.OwnerId.Value != element.Id.Value:
            raise ValueError("The existing finish sketch could not be verified.")
        evidence["profile"] = sorted(sorted(native_edge(curve) for curve in loop) for loop in sketch.Profile)
        offset = "FLOOR_HEIGHTABOVELEVEL_PARAM" if kind == "floor" else "CEILING_HEIGHTABOVELEVEL_PARAM"
        evidence["offset_ft"] = parameter(element, offset, DB.StorageType.Double, "AsDouble")
    return evidence


def record_fields(pairs):
    record = dict(pairs)
    if len(record) != len(pairs):
        raise ValueError("A room-finish provenance record repeats a field.")
    return record


def records_by_room(document, selected):
    guid = Guid(SCHEMA_GUID)
    schema = Schema.Lookup(guid)
    if schema is None:
        return {}
    fields = list(schema.ListFields())
    if (schema.SchemaName != SCHEMA_NAME or len(fields) != 1 or fields[0].FieldName != "Payload"
            or fields[0].ValueType.FullName != "System.String" or fields[0].ContainerType != ContainerType.Simple):
        raise ValueError("The room-finish schema differs from the expected field contract.")
    records = {}
    collector = DB.FilteredElementCollector(document)
    try:
        for index, element in enumerate(collector.WherePasses(ExtensibleStorageFilter(guid))):
            if index >= MAX_RECORDS:
                raise ValueError("The room-finish provenance inventory is incomplete.")
            entity = element.GetEntity(schema)
            if not entity.IsValid():
                raise ValueError("A room-finish provenance entity is invalid.")
            encoded = entity.Get[String](fields[0])
            if not isinstance(encoded, TEXT) or len(encoded) > 4096:
                raise ValueError("A room-finish provenance record is unavailable.")
            record = json.loads(encoded, object_pairs_hook=record_fields)
            if (not isinstance(record, dict) or set(record) != FIELDS
                    or any(not isinstance(value, TEXT) or not value or len(value) > 500 for value in record.values())
                    or record["kind"] not in ("wall", "floor", "ceiling")
                    or any(not re.match(r"^[0-9a-f]{64}\Z", record[key])
                           for key in ("piece_key", "observed_sha256"))):
                raise ValueError("A room-finish provenance record is malformed.")
            if record["room_uid"] in selected:
                records.setdefault(record["room_uid"], []).append((record, element))
    finally:
        collector.Dispose()
    return records


def audit_existing_finishes(document, rows):
    selected = {row["room_uid"] for row in rows if row["status"] == "boundary_verified"}
    records = records_by_room(document, selected)
    for row in rows:
        if row["status"] != "boundary_verified":
            continue
        existing = records.get(row["room_uid"], [])
        audit = {"status": "conflict", "tagged_pieces": len(existing)}
        row["existing_finishes"] = audit
        if row.get("wall_finish", {}).get("status", "reported") != "reported":
            audit["reason"] = "wall_preview_incomplete"
            continue
        try:
            expected = expected_pieces(row)
            audit["expected_piece_keys"] = sorted(expected)
            if not existing:
                audit["status"] = "no_tagged_pieces"
                continue
            if sorted(record["piece_key"] for record, _ in existing) != sorted(expected):
                audit["reason"] = "tagged_piece_set_differs"
                continue
            for record, element in existing:
                intended = expected[record["piece_key"]]
                observed = observed_piece(document, element, record["kind"])
                if record["kind"] == "wall":
                    geometry_matches = (observed["centerline"] == intended["centerline"]
                                        and observed["room_bounding"] == 0 and not any(observed["joined_ends"]))
                else:
                    geometry_matches = (observed["profile"] == intended["profile"]
                                        and abs(observed["offset_ft"] - intended["offset_ft"]) <= 1e-6)
                if (record["element_uid"] != str(element.UniqueId)
                        or any(record[key] != intended[key] or observed[key] != intended[key]
                               for key in ("kind", "type_uid", "level_uid"))
                        or observed["phase_uid"] != intended["phase_uid"]
                        or not geometry_matches
                        or record["observed_sha256"] != digest(observed)):
                    audit["reason"] = "tagged_piece_changed_or_copied"
                    break
            else:
                audit["status"] = "tagged_set_matches"
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            audit["reason"] = "tagged_piece_evidence_unavailable"
