# -*- coding: utf-8 -*-
"""Placement transaction for reviewed room finishes, pending native host qualification."""

import json
import math

from Autodesk.Revit.DB.ExtensibleStorage import Entity, Schema
from pyrevit.api import DB
from System import Guid, Int64, String

from room_finish_provenance import SCHEMA_GUID, SCHEMA_NAME, digest, edge_key, expected_pieces, matches_piece, native_edge, observed_piece, payload_field


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


def check_schema():
    """Check the field contract and a detached entity without changing the document."""
    guid = Guid(SCHEMA_GUID)
    schema = Schema.Lookup(guid)
    if schema is None:
        from Autodesk.Revit.DB.ExtensibleStorage import AccessLevel, SchemaBuilder
        from clr import GetClrType

        builder = SchemaBuilder(guid)
        try:
            builder.SetSchemaName(SCHEMA_NAME)
            builder.SetReadAccessLevel(AccessLevel.Public)
            builder.SetWriteAccessLevel(AccessLevel.Public)
            builder.AddSimpleField("Payload", GetClrType(String))
            schema = builder.Finish()
        finally:
            builder.Dispose()
    field = payload_field(schema)
    entity = Entity(schema)
    probe = "room-finish-schema-check"
    entity.Set[String](field, probe)
    if not entity.IsValid() or entity.Get[String](field) != probe:
        raise ValueError("Revit could not round-trip the room-finish schema.")
    return {"status": "schema_checked", "schema_guid": SCHEMA_GUID}


def finish_preview(document, request):
    from read_room_finish_boundaries import ids_from, read_boundaries

    if not isinstance(request, dict) or set(request) - {
            "room_ids", "horizontal_finishes", "wall_type_id", "wall_dimensions"}:
        raise ValueError("Supply the reviewed room and finish type scope.")
    if document is None or document.IsFamilyDocument or document.Application.VersionNumber != "2026":
        raise ValueError("Open the saved Revit 2026 project for this finish recipe.")
    if request.get("wall_type_id") is not None and request.get("wall_dimensions") is None:
        raise ValueError("Include wall height and base offset before previewing placement.")
    view = document.ActiveView
    if view is None or view.IsTemplate or not view.UniqueId:
        raise ValueError("Open a model view with a known phase.")
    phase = view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE)
    if phase is None or phase.StorageType != DB.StorageType.ElementId or phase.AsElementId().Value <= 0:
        raise ValueError("The active view phase is unavailable.")
    if DB.DesignOption.GetActiveDesignOptionId(document).Value != -1:
        raise ValueError("Switch to the main model before previewing room finishes.")
    phase_id = str(phase.AsElementId().Value)
    report = read_boundaries(document, ids_from(request.get("room_ids")), request.get("horizontal_finishes"),
                             request.get("wall_type_id"), True, request.get("wall_dimensions"))
    basis = {"report": report, "view_uid": str(view.UniqueId), "phase_id": phase_id,
             "workshared": bool(document.IsWorkshared)}
    return compute_preview(request, basis)


def compute_preview(request, basis):
    warnings, steps, after = [], [], []
    ownership_ids = set()
    for row in basis["report"]["rooms"]:
        room_id = str(row["room_id"])
        audit = row.get("existing_finishes", {})
        nearby = audit.get("nearby", {}).get("status")
        if row["status"] != "boundary_verified":
            warnings.append({"row": room_id, "text": row["reason"], "blocking": False})
        elif row["phase"]["id"] != basis["phase_id"]:
            warnings.append({"row": room_id, "text": "Room phase differs from the active view.", "blocking": False})
        elif nearby != "none_in_scope" or audit.get("status") not in ("no_tagged_pieces", "tagged_set_matches"):
            warnings.append({"row": room_id, "text": "Resolve existing or incomplete finish evidence.", "blocking": False})
        elif audit["status"] == "tagged_set_matches":
            warnings.append({"row": room_id, "text": "The reviewed finish set already exists.", "blocking": False})
        else:
            pieces = expected_pieces(row)
            if pieces:
                ownership_ids.add(room_id)
                ownership_ids.update(str(edge["source_id"]) for loop in row["boundary_loops"] for edge in loop
                                     if edge["source_id"] > 0 and edge["linked_source_id"] <= 0)
            for key, piece in sorted(pieces.items()):
                kind = piece["kind"]
                type_id = (row["wall_finish"]["type_id"] if kind == "wall" else
                           next(finish["type_id"] for finish in row["horizontal_finishes"] if finish["kind"] == kind))
                steps.append({"room_id": room_id, "piece_key": key, "type_id": type_id})
                after.append([key, room_id, kind, type_id, row["level"]["id"],
                              piece.get("offset_ft", piece.get("base_offset_ft")), piece.get("height_ft")])
    blocked = len(steps) > MAX_PIECES
    if blocked:
        warnings.append({"row": "scope", "text": "Preview at most 200 new finishes per run.", "blocking": True})
    return {"kind": "change-preview", "recipe": "room-finishes", "title": "Place reviewed room finishes",
            "request": request, "basis": basis, "basis_sha256": digest(basis),
            "columns": ["Piece", "Room ID", "Finish", "Type ID", "Level ID", "Offset ft", "Height ft"],
            "keyColumn": "Piece", "changedColumns": ["Finish"], "before": [], "after": after,
            "steps": [] if blocked else steps, "warnings": warnings, "blocked": blocked,
            "ownership_ids": sorted(ownership_ids, key=int),
            "notes": ["Review excluded rooms and all side effects before placement."]}


def room_profiles(room, held):
    from System.Collections.Generic import List

    options = DB.SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    profiles = List[DB.CurveLoop]()
    try:
        for boundary in room.GetBoundarySegments(options):
            profile = DB.CurveLoop()
            held.append(profile)
            for segment in boundary:
                profile.Append(segment.GetCurve())
            profiles.Add(profile)
    finally:
        options.Dispose()
    return profiles


def placement_entries(document, preview, held):
    selected = {}
    for step in preview["steps"]:
        selected.setdefault(step["room_id"], []).append(step)
    entries = []
    for row in preview["basis"]["report"]["rooms"]:
        steps = selected.get(str(row["room_id"]), [])
        if not steps:
            continue
        room = document.GetElement(native_id(str(row["room_id"])))
        if not isinstance(room, DB.Architecture.Room) or str(room.UniqueId) != row["room_uid"]:
            raise ValueError("A reviewed room changed identity.")
        shapes = expected_pieces(row)
        profiles = room_profiles(room, held) if row["horizontal_finishes"] else None
        centerlines = {}
        for loop in row["boundary_loops"]:
            for segment in loop:
                wall = segment.get("wall_finish", {})
                for footprint in wall.get("finish_footprints", []):
                    points = footprint["centerline_ft"]
                    centerlines[wall["wall_uid"], digest(edge_key("Line", *points))] = points
        for step in steps:
            intended = shapes[step["piece_key"]]
            entry = dict(step, intended=intended, room_uid=row["room_uid"], level_id=row["level"]["id"])
            if intended["kind"] == "wall":
                entry["centerline_ft"] = centerlines[intended["source_wall_uid"], digest(intended["centerline"])]
            else:
                entry["profiles"] = profiles
            entries.append(entry)
    return entries


def apply_preview(document, approved):
    current = finish_preview(document, approved["request"])
    # Ownership tooling can append warnings and notes to the displayed plan.
    fields = set(current) - {"warnings", "notes"}
    if (any(current[key] != approved.get(key) for key in fields) or current["blocked"]
            or any(warning.get("blocking") for warning in approved["warnings"])):
        raise ValueError("The room finish scope or preview changed.")
    if not current["steps"]:
        return {"status": "no_changes"}
    if current["basis"]["workshared"]:
        for value in current["ownership_ids"]:
            identifier = native_id(value)
            owner = str(DB.WorksharingUtils.GetCheckoutStatus(document, identifier))
            updates = str(DB.WorksharingUtils.GetModelUpdatesStatus(document, identifier))
            if (owner not in ("NotOwned", "OwnedByCurrentUser")
                    or updates not in ("CurrentWithCentral", "NotYetInCentral")):
                raise ValueError("A room finish source changed in central or is owned by another user.")
    held = []
    try:
        return place_room_finishes(document, placement_entries(document, current, held))
    finally:
        for profile in held:
            profile.Dispose()


if "inputs" in globals():
    try:
        mode = inputs.get("mode", "preview")
        if mode == "schema-check":
            result = check_schema()
        elif mode == "preview":
            plan = finish_preview(doc, inputs["request"])
            result = {"status": "ok", "preview_json": json.dumps(plan, ensure_ascii=True, allow_nan=False)}
        elif mode == "apply":
            result = apply_preview(doc, inputs["plan"])
        else:
            raise ValueError("Choose schema-check, preview, or apply.")
    except (ValueError, KeyError, TypeError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not establish the room finish scope.",
                  "error_type": type(error).__name__}
