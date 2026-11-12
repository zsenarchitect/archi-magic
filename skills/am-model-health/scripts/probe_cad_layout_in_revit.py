"""Probe native CAD layout placement in a transaction that always rolls back."""

import json
import math
from time import monotonic
from uuid import uuid4

from pyrevit.api import DB

from cad_layout_preview import edges, fields, inside, point_distance
from measure_cad_wall_openings import measure_walls_and_doors
from preview_cad_layout_in_revit import read_preview
from read_cad_room_boundaries import read_room, xyz_mm
from read_linked_cad_layout import MM_PER_FOOT, element_id

MAX_CIRCUITS = 500
MAX_SECONDS = 15


def xyz(point, z):
    return DB.XYZ(point[0] / MM_PER_FOOT, point[1] / MM_PER_FOOT, z / MM_PER_FOOT)


def check_time(deadline):
    if monotonic() >= deadline:
        raise ValueError("The CAD placement probe exceeded its time budget.")


def tracked(element, created):
    if element is None or element.Id.Value <= 0:
        raise ValueError("Revit did not create an identifiable element.")
    created.append(element.Id)
    return element


def typed_parameter(element, name, storage):
    parameter = element.get_Parameter(getattr(DB.BuiltInParameter, name))
    if parameter is None or not parameter.HasValue or parameter.StorageType != storage:
        raise ValueError("A placement parameter is unavailable.")
    return parameter


def set_integer(element, name, value):
    parameter = typed_parameter(element, name, DB.StorageType.Integer)
    if parameter.AsInteger() == value:
        return
    if parameter.IsReadOnly or not parameter.Set(value) or parameter.AsInteger() != value:
        raise ValueError("Revit did not retain a placement parameter.")


def require_phase(element, phase):
    if element.CreatedPhaseId.Value != phase.Id.Value:
        element.CreatedPhaseId = phase.Id
    if (element.CreatedPhaseId.Value != phase.Id.Value
            or element.DemolishedPhaseId.Value != DB.ElementId.InvalidElementId.Value):
        raise ValueError("Revit did not retain the reviewed creation phase.")


def install_failure_guard(transaction):
    class PlacementGuard(DB.IFailuresPreprocessor):
        __namespace__ = "ArchiMagic.CadLayout." + uuid4().hex

        def PreprocessFailures(self, accessor):
            try:
                failures = accessor.GetFailureMessages()
                if len(failures) == 0:
                    return DB.FailureProcessingResult.Continue
            except Exception:
                return DB.FailureProcessingResult.ProceedWithRollBack
            return DB.FailureProcessingResult.ProceedWithRollBack

    guard = PlacementGuard()
    options = transaction.GetFailureHandlingOptions()
    try:
        options.SetFailuresPreprocessor(guard)
        options.SetClearAfterRollback(True)
        transaction.SetFailureHandlingOptions(options)
    finally:
        options.Dispose()
    installed = transaction.GetFailureHandlingOptions()
    try:
        if installed.GetFailuresPreprocessor() != guard or not installed.GetClearAfterRollback():
            raise ValueError("Revit did not install the rollback failure guard.")
    finally:
        installed.Dispose()
    return guard


def wall_and_door_probe(document, preview, level, phase, created, deadline):
    proposal = preview["proposal"]
    walls, doors = {}, []
    center = int(DB.WallLocationLine.WallCenterline)
    for row in proposal["walls"]:
        check_time(deadline)
        choice = preview["wall_choices"][row["id"]]
        type_id = element_id(row["type_id"])
        a, b = (xyz(point, proposal["level_mm"]) for point in row["axis"])
        curve = DB.Line.CreateBound(a, b)
        try:
            wall = tracked(DB.Wall.Create(document, curve, type_id, level.Id, choice["height_mm"] / MM_PER_FOOT,
                                          0.0, False, False), created)
        finally:
            curve.Dispose()
        require_phase(wall, phase)
        set_integer(wall, "WALL_KEY_REF_PARAM", center)
        set_integer(wall, "WALL_ATTR_ROOM_BOUNDING", int(choice["room_bounding"]))
        for end in (0, 1):
            DB.WallUtils.DisallowWallJoinAtEnd(wall, end)
        walls[row["id"]] = wall
    for row in proposal["doors"]:
        check_time(deadline)
        type_id = element_id(row["type_id"])
        symbol = document.GetElement(type_id)
        if not isinstance(symbol, DB.FamilySymbol):
            raise ValueError("The reviewed door type is unavailable.")
        if not symbol.IsActive:
            symbol.Activate()
            document.Regenerate()
        host = walls[row["host"]]
        door = tracked(document.Create.NewFamilyInstance(
            xyz(row["location"], proposal["level_mm"]), symbol, host, level, DB.Structure.StructuralType.NonStructural),
            created)
        require_phase(door, phase)
        choice = preview["door_choices"][row["source_ids"][0]]
        if bool(door.HandFlipped) != choice["hand_flipped"] and (not door.CanFlipHand or not door.flipHand()):
            raise ValueError("The door cannot match the reviewed hand.")
        if bool(door.FacingFlipped) != choice["facing_flipped"] and (not door.CanFlipFacing or not door.flipFacing()):
            raise ValueError("The door cannot match the reviewed facing.")
        doors.append(door)
    document.Regenerate()
    tolerance = proposal["tolerance_mm"]
    for row in proposal["walls"]:
        check_time(deadline)
        wall = walls[row["id"]]
        choice = preview["wall_choices"][row["id"]]
        curve = wall.Location.Curve
        axis = [xyz_mm(curve.GetEndPoint(index)) for index in (0, 1)]
        expected = [(*point, proposal["level_mm"]) for point in row["axis"]]
        height = typed_parameter(wall, "WALL_USER_HEIGHT_PARAM", DB.StorageType.Double).AsDouble() * MM_PER_FOOT
        offset = typed_parameter(wall, "WALL_BASE_OFFSET", DB.StorageType.Double).AsDouble() * MM_PER_FOOT
        bounding = typed_parameter(wall, "WALL_ATTR_ROOM_BOUNDING", DB.StorageType.Integer).AsInteger()
        width = float(wall.Width) * MM_PER_FOOT
        if (not all(math.isfinite(value) for value in (height, offset, width))
                or wall.GetTypeId().Value != element_id(row["type_id"]).Value or wall.LevelId.Value != level.Id.Value
                or abs(width - row["width_mm"]) > tolerance or abs(height - choice["height_mm"]) > tolerance
                or abs(offset) > tolerance or bounding != int(choice["room_bounding"])
                or any(math.dist(a, b) > tolerance for a, b in zip(sorted(axis), sorted(expected)))
                or any(DB.WallUtils.IsWallJoinAllowedAtEnd(wall, end) for end in (0, 1))):
            raise ValueError("The new wall differs from its reviewed geometry, constraints, or joins.")
    for door, row in zip(doors, proposal["doors"]):
        check_time(deadline)
        choice = preview["door_choices"][row["source_ids"][0]]
        if (door.Host is None or door.Host.Id.Value != walls[row["host"]].Id.Value
                or door.GetTypeId().Value != element_id(row["type_id"]).Value or door.LevelId.Value != level.Id.Value
                or math.dist(xyz_mm(door.Location.Point), (*row["location"], proposal["level_mm"])) > tolerance
                or bool(door.HandFlipped) != choice["hand_flipped"] or bool(door.FacingFlipped) != choice["facing_flipped"]):
            raise ValueError("The new door differs from the reviewed host, type, level, point, or orientation.")
    return walls


def room_probe(document, proposal, walls, level, phase, created, deadline):
    view = document.ActiveView
    z, tolerance = proposal["level_mm"], proposal["tolerance_mm"]
    room_edges = []
    plane = None
    for room in proposal["rooms"]:
        check_time(deadline)
        allowed = {walls[edge["wall_id"]].Id.Value for edge in room["boundary_edges"] if edge["kind"] == "wall_face"}
        separators = [edge["edge"] for edge in room["boundary_edges"] if edge["kind"] == "separator"]
        if separators:
            if plane is None:
                geometry = DB.Plane.CreateByNormalAndOrigin(DB.XYZ.BasisZ, xyz((0, 0), z))
                try:
                    plane = tracked(DB.SketchPlane.Create(document, geometry), created)
                finally:
                    geometry.Dispose()
            curves = DB.CurveArray()
            for a, b in separators:
                curves.Append(DB.Line.CreateBound(xyz(a, z), xyz(b, z)))
            new_lines = [tracked(element, created) for element in document.Create.NewRoomBoundaryLines(plane, curves, view)]
            if len(new_lines) != len(separators):
                raise ValueError("Revit did not create every reviewed room separator.")
            allowed.update(element.Id.Value for element in new_lines)
        room_edges.append(allowed)
    document.Regenerate()
    options = DB.SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    circuits_seen = 0
    remaining_segments = [5000]
    try:
        for row, allowed in zip(proposal["rooms"], room_edges):
            check_time(deadline)
            contour = edges(row["boundary"])
            choices = []
            for circuit in document.get_PlanTopology(level, phase).Circuits:
                check_time(deadline)
                circuits_seen += 1
                if circuits_seen > MAX_CIRCUITS:
                    raise ValueError("The room circuit scan exceeded its budget.")
                if circuit.IsRoomLocated:
                    continue
                native = circuit.GetPointInside()
                point = (float(native.U) * MM_PER_FOOT, float(native.V) * MM_PER_FOOT)
                if inside(point, contour) and all(point_distance(point, edge) > tolerance for edge in contour):
                    choices.append(circuit)
            if len(choices) != 1:
                raise ValueError("The proposed room does not match one unoccupied native circuit.")
            unplaced = tracked(document.Create.NewRoom(phase), created)
            room = document.Create.NewRoom(unplaced, choices[0])
            if room is None or room.Id.Value != unplaced.Id.Value or room.LevelId.Value != level.Id.Value:
                raise ValueError("Revit did not place the phase-specific room on the reviewed level.")
            document.Regenerate()
            room.Location.Point = xyz(row["interior_point"], xyz_mm(room.Location.Point)[2])
            document.Regenerate()
            candidate = {"id": str(room.Id.Value), "uid": str(room.UniqueId), "version_guid": str(room.VersionGuid), "near": []}
            evidence = read_room(document, candidate, options, deadline, remaining_segments, include_loops=True)
            loops = evidence["boundary_loops"]
            if (evidence["phase_id"] != str(phase.Id.Value) or not room.IsPointInRoom(room.Location.Point)
                    or len(loops) != 1 or len(loops[0]) != len(contour)):
                raise ValueError("The placed room does not match the reviewed phase, seed, or contour.")
            actual = []
            for source, points in loops[0]:
                if int(source[0]) not in allowed or source[2] or len(points) != 2:
                    raise ValueError("The new room has an unreviewed finish boundary source.")
                actual.append(sorted(point[:2] for point in points))
            expected = [sorted(edge) for edge in contour]
            for edge in expected:
                matches = [line for line in actual if all(math.dist(a, b) <= tolerance for a, b in zip(line, edge))]
                if len(matches) != 1:
                    raise ValueError("The new room finish edges do not match the reviewed outline.")
                actual.remove(matches[0])
    finally:
        options.Dispose()


def create_probe_geometry(document, preview, created):
    deadline = monotonic() + MAX_SECONDS
    level = document.GetElement(element_id(preview["level_id"]))
    phase = document.GetElement(element_id(preview["view"]["phase_id"]))
    if not isinstance(level, DB.Level) or not isinstance(phase, DB.Phase):
        raise ValueError("The reviewed level or phase is unavailable.")
    walls = wall_and_door_probe(document, preview, level, phase, created, deadline)
    if preview["proposal"]["rooms"]:
        room_probe(document, preview["proposal"], walls, level, phase, created, deadline)
    measurements = measure_walls_and_doors(preview, walls, deadline)
    check_time(deadline)
    return measurements


def probe(document, request):
    fields(request, "mode preview preview_sha256")
    if request["mode"] != "rollback_probe":
        raise ValueError("Use rollback_probe on a disposable project.")
    if document is None or document.IsReadOnly or document.IsModifiable or document.IsWorkshared:
        raise ValueError("Use a writable, non-workshared disposable project without an active transaction.")
    baseline = read_preview(document, request["preview"])
    if baseline["status"] != "proposed" or baseline["preview_sha256"] != request["preview_sha256"]:
        raise ValueError("The full reviewed preview differs from the current project.")
    preview = json.loads(baseline["preview_json"])
    if (preview["provenance"]["status"] != "no_tags"
            or preview["existing_model_scan"]["status"] != "none_found"):
        raise ValueError("Resolve existing model candidates and provenance before probing placement.")
    transaction = DB.Transaction(document, "Probe reviewed CAD layout")
    created = []
    if transaction.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the placement probe."}
    failure = None
    rolled_back = False
    try:
        guard = install_failure_guard(transaction)
        if guard is None:
            raise ValueError("The rollback failure guard is unavailable.")
        measurements = create_probe_geometry(document, preview, created)
    except Exception as error:
        failure = {"error_type": type(error).__name__}
        if type(error) is ValueError:
            failure["message"] = str(error)
    finally:
        try:
            rolled_back = (transaction.GetStatus() == DB.TransactionStatus.Started
                           and transaction.RollBack() == DB.TransactionStatus.RolledBack
                           and transaction.GetStatus() == DB.TransactionStatus.RolledBack)
        except Exception:
            rolled_back = False
    try:
        if (not rolled_back or any(document.GetElement(identifier) is not None for identifier in created)
                or read_preview(document, request["preview"]) != baseline):
            raise ValueError("The probe rollback could not be verified.")
    except Exception:
        return {"status": "transaction_unresolved", "message": "Check the probe transaction and the model in Revit."}
    if failure:
        return {"status": "stopped", "rollback_verified": True, **failure}
    return {"status": "probe_rolled_back", "rollback_verified": True,
            "preview_sha256": baseline["preview_sha256"], "transient_element_count": len(created),
            "measurements": measurements}


if "inputs" in globals():
    try:
        result = probe(doc, inputs)
    except (ValueError, KeyError, TypeError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "transaction_unresolved", "error_type": type(error).__name__}
