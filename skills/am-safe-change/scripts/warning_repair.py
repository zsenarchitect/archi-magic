# -*- coding: utf-8 -*-
"""Read and apply reviewed warning repair pairs through the pyRevit Agent Runtime."""
import hashlib
import json
import math
from uuid import uuid4

from System import Int64
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

import warning_repair_preview as preview


def key(identifier):
    return str(identifier.Value)


def identifier(value):
    return DB.ElementId(Int64(int(preview.element_id(value))))


def project_identity(document):
    if document is None or document.IsFamilyDocument or document.IsReadOnly or not document.PathName:
        raise ValueError("Open a saved, writable Revit project.")
    project = document.ProjectInformation
    if project is None or not project.UniqueId:
        raise ValueError("The project identity is unavailable.")
    return {"project_uid": str(project.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}


def clock_check(clock):
    if clock.ElapsedMilliseconds > 10000:
        raise ValueError("The native repair read exceeded its ten-second budget.")


def native_types():
    overlap = DB.BuiltInFailures.OverlapFailures
    performance = DB.BuiltInFailures.PerformanceFailures
    return {str(overlap.DuplicateInstances.Guid): "duplicate-furniture",
            str(performance.DuplicateInstances.Guid): "duplicate-furniture",
            str(overlap.RoomSeparationLinesOverlap.Guid): "coincident-room-separators"}


def native_warnings(document, clock):
    messages = document.GetWarnings()
    clock_check(clock)
    if len(messages) > preview.MAX_WARNINGS:
        raise ValueError("More than 5,000 warnings. The warning inventory is incomplete.")
    rows = []
    references = 0
    for message in messages:
        failing = message.GetFailingElements()
        additional = message.GetAdditionalElements()
        clock_check(clock)
        references += len(failing) + len(additional)
        if references > preview.MAX_REFERENCES:
            raise ValueError("More than 20,000 warning element references.")
        rows.append([str(message.GetFailureDefinitionId().Guid),
                     [key(item) for item in failing], [key(item) for item in additional]])
    return preview.warning_rows(preview.warning_multiset(rows))


def coordinates(point):
    return tuple(float(value) for value in (point.X, point.Y, point.Z))


def option_id(element):
    return key(element.DesignOption.Id) if element.DesignOption is not None else "-1"


def ownership(document, element):
    if not document.IsWorkshared:
        return None
    status = str(DB.WorksharingUtils.GetCheckoutStatus(document, element.Id))
    updates = str(DB.WorksharingUtils.GetModelUpdatesStatus(document, element.Id))
    return [status, updates]


def writable(state):
    return state is None or (state[0] in ("NotOwned", "OwnedByCurrentUser")
                             and state[1] in ("CurrentWithCentral", "NotYetInCentral"))


def typed_parameters(element, budget, clock):
    rows = []
    for parameter in element.Parameters:
        clock_check(clock)
        budget["parameters"] -= 1
        if budget["parameters"] < 0:
            raise ValueError("More than 4,000 parameter values in the repair batch.")
        storage = str(parameter.StorageType)
        if storage == "None":
            raise ValueError("An instance parameter has an unsupported storage type.")
        getter = {"Integer": "AsInteger", "Double": "AsDouble", "String": "AsString", "ElementId": "AsElementId"}.get(storage)
        if getter is None:
            raise ValueError("An instance parameter has an unsupported storage type.")
        value = getattr(parameter, getter)() if parameter.HasValue else None
        if storage == "ElementId" and value is not None:
            value = key(value)
        shared_guid = str(parameter.GUID) if parameter.IsShared else None
        rows.append([key(parameter.Id), shared_guid, storage, value])
    # Use the parameter identity and typed value, including repeated IDs.
    return sorted(rows, key=lambda row: json.dumps(row, ensure_ascii=True, allow_nan=False))


def empty_relationships(element, budget, clock):
    dependents = element.GetDependentElements(None)
    entities = element.GetEntitySchemaGuids()
    clock_check(clock)
    budget["relationships"] -= len(dependents) + len(entities)
    if budget["relationships"] < 0:
        raise ValueError("More than 4,000 relationship records in the repair batch.")
    return not len(dependents) and not len(entities)


def furniture_state(document, element):
    if (not isinstance(element, DB.FamilyInstance)
            or element.Category.Id.Value != int(DB.BuiltInCategory.OST_Furniture)
            or not isinstance(element.Location, DB.LocationPoint)
            or element.Symbol is None or element.Symbol.Family.IsInPlace
            or element.Symbol.Family.FamilyPlacementType != DB.FamilyPlacementType.OneLevelBased
            or element.Host is not None or element.HostFace is not None or element.SuperComponent is not None
            or element.MEPModel is not None or len(element.GetSubComponentIds())):
        return None
    level = document.GetElement(element.LevelId)
    if not isinstance(level, DB.Level):
        return None
    transform = element.GetTransform()
    return {"level": key(level.Id), "symbol": key(element.Symbol.Id),
            "point": coordinates(element.Location.Point), "rotation": float(element.Location.Rotation),
            "transform": [coordinates(value) for value in
                          (transform.Origin, transform.BasisX, transform.BasisY, transform.BasisZ)],
            "flip_mirror": [bool(element.FacingFlipped), bool(element.HandFlipped), bool(element.Mirrored)]}


def separator_state(document, element, clock):
    if (not isinstance(element, DB.ModelLine)
            or element.Category.Id.Value != int(DB.BuiltInCategory.OST_RoomSeparationLines)
            or element.SketchPlane is None
            or any(len(element.GetAdjoinedCurveElements(end)) for end in (0, 1))):
        return None
    curve = element.GeometryCurve
    if not isinstance(curve, DB.Line) or not curve.IsBound:
        return None
    plane = element.SketchPlane.GetPlane()
    normal = coordinates(plane.Normal)
    if normal not in ((0.0, 0.0, 1.0), (0.0, 0.0, -1.0)):
        return None
    elevation = float(plane.Origin.Z)
    level = document.GetElement(element.LevelId) if element.LevelId.Value > 0 else None
    if level is None:
        matches = []
        for candidate in DB.FilteredElementCollector(document).OfClass(DB.Level):
            clock_check(clock)
            if abs(float(candidate.ProjectElevation) - elevation) <= 1e-7:
                matches.append(candidate)
                if len(matches) > 1:
                    return None
        level = matches[0] if matches else None
    if not isinstance(level, DB.Level) or abs(float(level.ProjectElevation) - elevation) > 1e-7:
        return None
    endpoints = sorted(coordinates(curve.GetEndPoint(end)) for end in (0, 1))
    if endpoints[0] == endpoints[1] or any(abs(point[2] - elevation) > 1e-7 for point in endpoints):
        return None
    return {"level": key(level.Id), "elevation": elevation, "sketch_plane": key(element.SketchPlane.Id),
            "endpoints": endpoints}


def describe_element(document, value, recipe, budget, clock):
    element = document.GetElement(identifier(value))
    clock_check(clock)
    if element is None or element.Category is None:
        return {"eligible": False}
    result = {"uid": str(element.UniqueId), "recipe": recipe, "eligible": False}
    if (element.Pinned or element.ViewSpecific or element.GroupId.Value != -1
            or element.AssemblyInstanceId.Value != -1 or not empty_relationships(element, budget, clock)):
        return result
    if recipe == "duplicate-furniture":
        shape = furniture_state(document, element)
    elif recipe == "coincident-room-separators":
        shape = separator_state(document, element, clock)
    else:
        return result
    if shape is None:
        return result
    state = ownership(document, element)
    context = {"workset": str(element.WorksetId.IntegerValue), "option": option_id(element),
               "created_phase": key(element.CreatedPhaseId), "demolished_phase": key(element.DemolishedPhaseId)}
    result.update(eligible=True, writable=writable(state), ownership=state,
                  equivalence={"shape": shape, "context": context,
                               "parameters": typed_parameters(element, budget, clock)})
    clock_check(clock)
    return result


def canonical_loop(edges):
    if len(edges) < 2:
        raise ValueError("A room boundary loop is incomplete.")
    starts = [edge[2][0] for edge in edges]
    if len(set(starts)) != len(starts):
        raise ValueError("A room boundary loop repeats a start point.")
    for index, (_, _, points) in enumerate(edges):
        start = starts[(index + 1) % len(edges)]
        if sum((a - b) ** 2 for a, b in zip(points[-1], start)) > 1e-14:
            raise ValueError("A room boundary loop is open.")

    def rotate(rows):
        index = min(range(len(rows)), key=lambda i: rows[i][2][0])
        return rows[index:] + rows[:index]

    reverse = [(source, linked, tuple(reversed(points))) for source, linked, points in reversed(edges)]
    return min(rotate(edges), rotate(reverse))


def room_inventory(document, clock, aliases=None):
    aliases = aliases or {}
    rooms = {}
    room_count = 0
    segment_count = 0
    options = DB.SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = DB.SpatialElementBoundaryLocation.Finish
    try:
        collector = DB.FilteredElementCollector(document).OfCategory(DB.BuiltInCategory.OST_Rooms)
        for room in collector.WhereElementIsNotElementType():
            clock_check(clock)
            room_count += 1
            if room_count > 1000:
                raise ValueError("More than 1,000 rooms. The room census is incomplete.")
            if not isinstance(room, DB.Architecture.Room):
                raise ValueError("The room census found an unsupported element.")
            location = room.Location
            if location is None:
                continue
            area = float(room.Area)
            if math.isnan(area) or math.isinf(area) or area < 0:
                raise ValueError("A placed room has an invalid area.")
            loops = room.GetBoundarySegments(options)
            clock_check(clock)
            if (loops is not None and len(loops) > 0) != (area > 0):
                raise ValueError("A room's area and boundary availability disagree.")
            rows = []
            for loop in loops or []:
                segment_count += len(loop)
                if segment_count > 10000:
                    raise ValueError("More than 10,000 room boundary segments.")
                edges = []
                for segment in loop:
                    clock_check(clock)
                    source, linked = key(segment.ElementId), key(segment.LinkElementId)
                    curve = segment.GetCurve()
                    if not isinstance(curve, (DB.Line, DB.Arc)) or not curve.IsBound:
                        raise ValueError("A room has boundary geometry that cannot be verified.")
                    points = [coordinates(curve.GetEndPoint(0))]
                    if isinstance(curve, DB.Arc):
                        points.append(coordinates(curve.Evaluate(0.5, True)))
                    points.append(coordinates(curve.GetEndPoint(1)))
                    if linked == "-1":
                        source = aliases.get(source, source)
                    edges.append((source, linked, tuple(points)))
                rows.append(canonical_loop(edges))
            room_id = key(room.Id)
            if room_id in rooms or room.LevelId.Value <= 0 or not room.UniqueId:
                raise ValueError("A placed room has no verifiable identity or level.")
            rooms[room_id] = {"uid": str(room.UniqueId), "location": coordinates(location.Point),
                              "level": key(room.LevelId), "phase": key(room.CreatedPhaseId),
                              "option": option_id(room), "enclosed": bool(rows), "area": area,
                              "loops": sorted(rows)}
        clock_check(clock)
        return rooms
    finally:
        options.Dispose()


def read_snapshot(document, request):
    clock = Stopwatch.StartNew()
    identity = project_identity(document)
    allowed = native_types()
    pairs = request["pairs"]
    if not isinstance(pairs, list) or not 1 <= len(pairs) <= preview.MAX_PAIRS:
        raise ValueError("Choose one to 20 explicit keep and remove pairs.")
    members = [preview.element_id(pair[role]) for pair in pairs for role in ("keep_id", "remove_id")]
    if len(set(members)) != len(members):
        raise ValueError("Repair pairs must be disjoint.")
    warnings = native_warnings(document, clock)
    elements = {}
    budget = {"parameters": 4000, "relationships": 4000}
    for pair in pairs:
        recipe = allowed.get(preview.failure_id(pair["failure_id"]))
        for field in ("keep_id", "remove_id"):
            item = pair[field]
            elements[item] = describe_element(document, item, recipe, budget, clock)
    needs_rooms = any(allowed.get(pair["failure_id"]) == "coincident-room-separators" for pair in pairs)
    rooms = room_inventory(document, clock) if needs_rooms else None
    snapshot = {"kind": "warning-repair-snapshot", "complete": True, "document": identity,
                "request": request, "warnings": warnings, "repair_types": allowed, "elements": elements,
                "room_inventory_complete": needs_rooms, "rooms": rooms}
    preview.snapshot_digest(snapshot)
    clock_check(clock)
    return snapshot


TRANSACTION_NAME = "Repair reviewed Revit warnings"


def approved_snapshot(document, plan, display, approved_borrowing):
    snapshot = read_snapshot(document, plan["request"])
    if not isinstance(approved_borrowing, list) or len(approved_borrowing) > preview.MAX_PAIRS:
        raise ValueError("Supply at most 20 reviewed borrowing IDs.")
    ids = set(map(preview.element_id, approved_borrowing))
    if len(ids) != len(approved_borrowing) or not ids.issubset(plan["ownership_ids"]):
        raise ValueError("Review borrowing for approved removal IDs only.")
    live_ownership = {}
    actual_borrowing = []
    for item in plan["ownership_ids"]:
        element = snapshot["elements"][item]
        current = element.get("ownership")
        prior = plan["removal_ownership"][item]
        live_ownership[item] = current
        if item in ids and prior is not None and current is not None:
            if prior[0] == "NotOwned" and current[0] == "OwnedByCurrentUser" and prior[1] == current[1]:
                element["ownership"] = prior
                actual_borrowing.append(item)
    if set(actual_borrowing) != ids:
        raise ValueError("The observed post-dry-run borrowing does not match the approved IDs.")
    recomputed_plan, recomputed_display = preview.compute(snapshot)
    if recomputed_plan["blocked"] or recomputed_plan != plan:
        raise ValueError("The approved repair plan no longer matches the model.")
    if display.get("blocked") is not False or any(warning.get("blocking") for warning in display["warnings"]):
        raise ValueError("The displayed warning repair preview is blocked.")
    for field, value in recomputed_display.items():
        if field != "warnings" and display.get(field) != value:
            raise ValueError("The displayed repair rows differ from the approved plan.")
    annotation = display.get("ownership")
    if annotation and (annotation["owned_by_others"] or annotation["updated_in_central"]):
        raise ValueError("The displayed preview reports a worksharing conflict.")
    return snapshot, live_ownership, actual_borrowing


def install_failure_guard(transaction):
    class RepairFailureGuard(DB.IFailuresPreprocessor):
        __namespace__ = "ArchiMagic.WarningRepair." + uuid4().hex

        def __init__(self):
            self.failure_count = 0
            self.failures = []

        def PreprocessFailures(self, accessor):
            try:
                failures = accessor.GetFailureMessages()
                self.failure_count += len(failures)
                for failure in failures:
                    if len(self.failures) == 20:
                        break
                    self.failures.append([str(failure.GetFailureDefinitionId().Guid), str(failure.GetSeverity())])
            except Exception:
                self.failure_count += 1
            if self.failure_count:
                return DB.FailureProcessingResult.ProceedWithRollBack
            return DB.FailureProcessingResult.Continue

    guard = RepairFailureGuard()
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
            raise ValueError("Revit did not install the transaction failure guard.")
    finally:
        installed.Dispose()
    return guard


def expected_room_state(rooms, aliases):
    if rooms is None:
        return None
    result = {}
    for item, room in rooms.items():
        loops = []
        for loop in room["loops"]:
            edges = [(aliases.get(source, source) if linked == "-1" else source, linked, points)
                     for source, linked, points in loop]
            loops.append(canonical_loop(edges))
        result[item] = dict(room, loops=sorted(loops))
    return result


def inspect_result(document, plan, clock, expect_rooms):
    if project_identity(document) != plan["document"]:
        raise ValueError("The repair result came from another project.")
    warnings = native_warnings(document, clock)
    if warnings != plan["expected_warnings"]:
        raise ValueError("The resulting warning inventory differs from the approved warning reductions.")
    kept = {}
    budget = {"parameters": 4000, "relationships": 4000}
    for step in plan["steps"]:
        item = step["keep_id"]
        state = describe_element(document, item, step["recipe"], budget, clock)
        if state.get("eligible") is not True or state.get("uid") != step["keep_uid"]:
            raise ValueError("A reviewed keeper is missing or changed identity.")
        kept[item] = state
        if document.GetElement(identifier(step["remove_id"])) is not None:
            raise ValueError("A reviewed removal is still present.")
        clock_check(clock)
    rooms = room_inventory(document, clock) if expect_rooms else None
    return kept, rooms, warnings


def rollback(transaction, group):
    try:
        state = transaction.GetStatus()
        if state == DB.TransactionStatus.Started:
            state = transaction.RollBack()
        if state == DB.TransactionStatus.Pending:
            return False
        group_state = group.GetStatus()
        if group_state == DB.TransactionStatus.Started:
            group_state = group.RollBack()
        return group_state == DB.TransactionStatus.RolledBack
    except Exception:
        return False


def apply(document, plan, display, approved_borrowing):
    snapshot, live_ownership, borrowed = approved_snapshot(document, plan, display, approved_borrowing)
    removals = set(plan["ownership_ids"])
    aliases = {step["remove_id"]: step["keep_id"] for step in plan["steps"]
               if step["recipe"] == "coincident-room-separators"}
    expected_rooms = expected_room_state(snapshot["rooms"], aliases)
    expected_keepers = {step["keep_id"]: snapshot["elements"][step["keep_id"]] for step in plan["steps"]}
    application = document.Application
    events = []

    def record_change(_sender, event):
        try:
            same_document = event.GetDocument().Equals(document)
            if not same_document:
                events.append("other_document")
                return
            if events or list(event.GetTransactionNames()) != [TRANSACTION_NAME]:
                events.append("unattributed")
                return
            added, modified = event.GetAddedElementIds(), event.GetModifiedElementIds()
            deleted = event.GetDeletedElementIds()
            if len(added) or len(modified) or len(deleted) != len(removals):
                events.append("unexpected")
                return
            events.append({key(item) for item in deleted})
        except Exception:
            events.append("unavailable")

    group = DB.TransactionGroup(document, TRANSACTION_NAME)
    transaction = DB.Transaction(document, TRANSACTION_NAME)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the repair group."}
    clock = Stopwatch.StartNew()
    guard = None
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the repair transaction.")
        guard = install_failure_guard(transaction)
        application.DocumentChanged += record_change
        try:
            for step in plan["steps"]:
                item = step["remove_id"]
                element = document.GetElement(identifier(item))
                if element is None or str(element.UniqueId) != step["remove_uid"]:
                    raise ValueError("A removal element changed identity before deletion.")
                state = ownership(document, element)
                if state != live_ownership[item] or not writable(state):
                    raise ValueError("Ownership or central state changed before deletion.")
                clock_check(clock)
                deleted = document.Delete(element.Id)
                if len(deleted) != 1 or key(next(iter(deleted))) != item:
                    raise ValueError("Revit returned an unapproved deletion set.")
                clock_check(clock)
            state = transaction.Commit()
        finally:
            application.DocumentChanged -= record_change
        if guard.failure_count:
            raise ValueError("Revit posted %d failure(s) during repair. Review the native warning or error."
                             % guard.failure_count)
        if state != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not commit the inner repair transaction.")
        if "other_document" in events:
            raise ValueError("Revit reported a change in another document. Review it separately.")
        if events != [removals]:
            raise ValueError("Revit did not report exactly the approved deletions with no other effects.")
        clock_check(clock)
        kept, rooms, warnings = inspect_result(document, plan, clock, expected_rooms is not None)
        if kept != expected_keepers or rooms != expected_rooms:
            raise ValueError("A survivor or room boundary differs from the approved state.")
        receipt = {"document": plan["document"], "basis_sha256": plan["basis_sha256"],
                   "deleted": sorted(removals, key=int), "kept_sha256": preview.snapshot_digest(kept),
                   "rooms_sha256": preview.snapshot_digest(rooms), "warnings": warnings,
                   "borrowed_after_dry_run": borrowed}
        payload = json.dumps(receipt, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        if len(payload) > preview.MAX_BYTES:
            raise ValueError("The repair receipt exceeds 2 MiB.")
        clock_check(clock)
        if group.Assimilate() != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not complete the repair group.")
        return {"status": "ok", "receipt_json": payload}
    except Exception as error:
        if rollback(transaction, group):
            return {"status": "stopped", "target_rollback_verified": True, "message": str(error),
                    "posted_failures": guard.failures if guard else []}
        return {"status": "transaction_unresolved", "message": "Check the Revit repair transaction before continuing.",
                "posted_failures": guard.failures if guard else []}


def main(document, args):
    mode = args.get("mode", "read")
    if mode == "apply":
        return apply(document, args["plan"], args["preview"], args.get("approved_borrowing", []))
    if mode != "read":
        raise ValueError("Choose read or apply for a native warning repair.")
    snapshot = read_snapshot(document, args["request"])
    return {"status": "ok", "snapshot_json": json.dumps(snapshot, ensure_ascii=True, sort_keys=True,
                                                        separators=(",", ":"), allow_nan=False)}


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not verify repair evidence (%s)."
                  % type(error).__name__}
