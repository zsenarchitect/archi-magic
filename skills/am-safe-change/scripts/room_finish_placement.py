# -*- coding: utf-8 -*-
"""Placement transaction for reviewed room finishes, pending native host qualification."""

import json
import math

from Autodesk.Revit.DB.ExtensibleStorage import Entity, Schema
from pyrevit.api import DB
from System import Guid, Int64, String

from room_finish_provenance import SCHEMA_GUID, digest, edge_key, matches_piece, native_edge, observed_piece, payload_field


MAX_PIECES = 200
HEIGHT_PARAMETERS = {"floor": "FLOOR_HEIGHTABOVELEVEL_PARAM", "ceiling": "CEILING_HEIGHTABOVELEVEL_PARAM"}


def native_id(value):
    number = int(value)
    if str(number) != value or not 0 < number < 2**63:
        raise ValueError("Use a positive decimal-string Revit ID.")
    return DB.ElementId(Int64(number))


def check_geometry(document, piece):
    intended = piece["intended"]
    kind = intended["kind"]
    if piece["piece_key"] != digest(intended) or not piece["room_uid"]:
        raise ValueError("The proposed finish identity differs from its reviewed evidence.")
    type_id = native_id(piece["type_id"])
    level_id = native_id(piece["level_id"])
    level, element_type = document.GetElement(level_id), document.GetElement(type_id)
    expected_type = {"wall": DB.WallType, "floor": DB.FloorType, "ceiling": DB.CeilingType}[kind]
    if (not isinstance(level, DB.Level) or str(level.UniqueId) != intended["level_uid"]
            or not isinstance(element_type, expected_type) or str(element_type.UniqueId) != intended["type_uid"]):
        raise ValueError("The selected finish level or type has changed.")
    if kind == "wall":
        a, b = piece["centerline_ft"]
        if (edge_key("Line", a, b) != intended["centerline"]
                or math.hypot(b[0] - a[0], b[1] - a[1]) <= document.Application.ShortCurveTolerance
                or not 0 < intended["height_ft"] <= 30000
                or not -30000 <= intended["base_offset_ft"] <= 30000
                or element_type.Kind != DB.WallKind.Basic):
            raise ValueError("The wall geometry or dimensions differ from the reviewed finish.")
        z = level.ProjectElevation
        geometry = DB.Line.CreateBound(DB.XYZ(a[0], a[1], z), DB.XYZ(b[0], b[1], z))
    else:
        geometry = piece["profiles"]
        actual = sorted(sorted(native_edge(curve) for curve in loop) for loop in geometry)
        if (actual != intended["profile"] or not DB.BoundaryValidation.IsValidHorizontalBoundary(geometry)
                or (kind == "floor" and element_type.IsFoundationSlab)):
            raise ValueError("The horizontal finish profile or type differs from the reviewed finish.")
    return kind, type_id, level_id, geometry


def set_parameter(element, name, storage, value):
    parameter = element.get_Parameter(getattr(DB.BuiltInParameter, name))
    if parameter is None or parameter.IsReadOnly or parameter.StorageType != storage:
        raise ValueError("The new finish parameter is unavailable.")
    parameter.Set(value)


def create_piece(document, prepared, intended):
    kind, type_id, level_id, geometry = prepared
    if kind == "wall":
        element = DB.Wall.Create(document, geometry, type_id, level_id,
                                 float(intended["height_ft"]), float(intended["base_offset_ft"]), False, False)
        set_parameter(element, "WALL_ATTR_ROOM_BOUNDING", DB.StorageType.Integer, 0)
        for end in (0, 1):
            DB.WallUtils.DisallowWallJoinAtEnd(element, end)
        return element
    creator = DB.Floor if kind == "floor" else DB.Ceiling
    element = creator.Create(document, geometry, type_id, level_id)
    set_parameter(element, HEIGHT_PARAMETERS[kind], DB.StorageType.Double, float(intended["offset_ft"]))
    return element


def write_provenance(document, element, piece, schema, field):
    intended = piece["intended"]
    observed = observed_piece(document, element, intended["kind"])
    if not element.UniqueId or not matches_piece(intended, observed):
        raise ValueError("The created finish does not match the reviewed geometry and type.")
    record = dict({key: intended[key] for key in ("kind", "type_uid", "level_uid")},
                  room_uid=piece["room_uid"], element_uid=str(element.UniqueId), piece_key=piece["piece_key"],
                  observed_sha256=digest(observed))
    encoded = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    entity = Entity(schema)
    entity.Set[String](field, encoded)
    element.SetEntity(entity)
    fresh = element.GetEntity(schema)
    if not fresh.IsValid() or fresh.Get[String](field) != encoded:
        raise ValueError("Revit did not retain the new finish provenance.")
    return {"id": str(element.Id.Value), "uid": record["element_uid"], "piece_key": piece["piece_key"]}


def place_room_finishes(document, pieces):
    """Place a batch already revalidated by its caller in the current document."""
    if document is None or document.IsFamilyDocument or document.IsReadOnly or document.IsModifiable:
        raise ValueError("Use a writable project without an active transaction.")
    if not 1 <= len(pieces) <= MAX_PIECES or len({(p["room_uid"], p["piece_key"]) for p in pieces}) != len(pieces):
        raise ValueError("Supply one to 200 distinct reviewed finish pieces.")
    schema = Schema.Lookup(Guid(SCHEMA_GUID))
    if schema is None:
        raise ValueError("The room-finish provenance schema must be qualified before placement.")
    field = payload_field(schema)
    prepared = [check_geometry(document, piece) for piece in pieces]
    transaction = DB.Transaction(document, "Place reviewed room finishes")
    if transaction.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the finish transaction."}
    try:
        elements = [create_piece(document, entry, piece["intended"]) for entry, piece in zip(prepared, pieces)]
        document.Regenerate()
        receipt = [write_provenance(document, element, piece, schema, field)
                   for element, piece in zip(elements, pieces)]
        state = transaction.Commit()
        if state == DB.TransactionStatus.Committed:
            return {"status": "ok", "created": receipt}
        if state == DB.TransactionStatus.RolledBack:
            return {"status": "stopped", "target_rollback_verified": True}
    except Exception as error:
        try:
            state = transaction.GetStatus()
            if state == DB.TransactionStatus.Started:
                state = transaction.RollBack()
        except Exception:
            return {"status": "transaction_unresolved", "message": "Revit could not confirm finish rollback."}
        if state == DB.TransactionStatus.RolledBack:
            return {"status": "stopped", "target_rollback_verified": True, "error_type": type(error).__name__}
    return {"status": "transaction_unresolved", "message": "Check the finish transaction in Revit."}
