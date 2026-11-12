"""Create and measure a room-view batch in a rollback-only Revit trial.

Use run_modify dry run in an approved, saved, non-workshared fixture. Pass the
reviewed preview's preflight inputs, its independently approved digest, and
the ID/UID of an unrelated fixture control. This script cannot keep a batch.
"""

import json
import math
from uuid import uuid4

from pyrevit.api import DB

import read_room_finish_boundaries as reader


TOLERANCE = 1e-5
MAX_EFFECTS = 160


def resolve(document, identity):
    element = reader.native_element(document, identity["id"])
    if element is None or element.UniqueId != identity["uid"]:
        raise ValueError("A reviewed native element identity changed.")
    return element


def horizontal_angle(first, second):
    if abs(first.Z) > TOLERANCE or abs(second.Z) > TOLERANCE:
        raise ValueError("Expected a horizontal elevation facing.")
    return math.atan2(first.X * second.Y - first.Y * second.X,
                      first.X * second.X + first.Y * second.Y)


def section_box(spec):
    origin, look, _ = reader.view_frame(spec)
    low, high = spec["crop_min"], spec["crop_max"]
    transform = DB.Transform.Identity
    transform.Origin = origin
    transform.BasisZ = look
    transform.BasisY = DB.XYZ.BasisZ
    transform.BasisX = transform.BasisY.CrossProduct(look)
    box = DB.BoundingBoxXYZ()
    box.Transform = transform
    box.Min = DB.XYZ(-high[0], low[1], 0)
    box.Max = DB.XYZ(-low[0], high[1], spec["far_extent"])
    box.Enabled = True
    for dimension in range(3):
        box.set_MinEnabled(dimension, True)
        box.set_MaxEnabled(dimension, True)
    return box


def marker_position(marker, origin, room):
    if not isinstance(marker.Location, DB.LocationPoint):
        raise ValueError("The elevation marker has no verifiable native point.")
    actual = marker.Location.Point
    witness = DB.XYZ(actual.X, actual.Y, origin.Z)
    if witness.DistanceTo(origin) > TOLERANCE or not room.IsPointInRoom(witness):
        raise ValueError("The native elevation marker moved off the reviewed point.")
    return actual


def create_view(document, spec, host, room, scale, additions):
    view_type = resolve(document, spec["type"]).Id
    origin, look, _ = reader.view_frame(spec)
    if spec["kind"] == "section":
        box = section_box(spec)
        try:
            view = DB.ViewSection.CreateSection(document, view_type, box)
        finally:
            box.Dispose()
        additions.add(str(view.Id.Value))
        return view, None, None

    marker = DB.ElevationMarker.CreateElevationMarker(document, view_type, origin, scale)
    additions.add(str(marker.Id.Value))
    slot = 0
    if not marker.IsAvailableIndex(slot):
        raise ValueError("The new elevation marker has no available trial slot.")
    view = marker.CreateElevation(document, host.Id, slot)
    additions.add(str(view.Id.Value))
    document.Regenerate()
    marker_position(marker, origin, room)
    angle = horizontal_angle(view.ViewDirection, look.Negate())
    if abs(angle) > TOLERANCE:
        axis = DB.Line.CreateBound(origin, origin.Add(DB.XYZ.BasisZ))
        try:
            DB.ElementTransformUtils.RotateElement(document, marker.Id, axis, angle)
        finally:
            axis.Dispose()
        document.Regenerate()
    marker_position(marker, origin, room)
    return view, marker, slot


def controlled_parameters(template):
    return {item.Value for item in template.GetTemplateParameterIds()} - {
        item.Value for item in template.GetNonControlledTemplateParameterIds()
    }


def parameter(view, name, storage):
    param = view.get_Parameter(getattr(DB.BuiltInParameter, name))
    if param is None or param.StorageType != storage:
        raise ValueError("The new view has no compatible %s parameter." % name)
    return param


def require_writable(param, controlled):
    if param.IsReadOnly or param.Id.Value in controlled:
        raise ValueError("The template or native view prevents a required setting change.")


def check_scope(view):
    scope = view.get_Parameter(DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
    if scope is not None and (scope.StorageType != DB.StorageType.ElementId
                              or scope.AsElementId().Value > 0):
        raise ValueError("A scope box overrides the proposed room crop.")


def frame_bounds(view, spec):
    origin, _, right = reader.view_frame(spec)
    crop = view.CropBox
    if crop is None:
        raise ValueError("The new view has no native crop frame.")
    transform = crop.Transform.Inverse
    low, high = spec["crop_min"], spec["crop_max"]
    points = [transform.OfPoint(origin.Add(right.Multiply(x)).Add(DB.XYZ.BasisZ.Multiply(y)))
              for x in (low[0], high[0]) for y in (low[1], high[1])]
    return crop, [min(point.X for point in points), min(point.Y for point in points)], [
        max(point.X for point in points), max(point.Y for point in points)
    ]


def read_crop(view, lower, upper):
    manager = view.GetCropRegionShapeManager()
    try:
        if manager.Split or manager.ShapeSet:
            raise ValueError("The view has a split or shaped crop.")
        loops = manager.GetCropShape()
        try:
            if loops.Count != 1:
                raise ValueError("The view needs one rectangular model crop.")
            expected = [(x, y) for x in (lower[0], upper[0]) for y in (lower[1], upper[1])]
            points = []
            for curve in loops[0]:
                if len(points) >= 4 or not isinstance(curve, DB.Line):
                    raise ValueError("The native model crop is not a rectangle.")
                first, second = curve.GetEndPoint(0), curve.GetEndPoint(1)
                dx, dy = abs(first.X - second.X), abs(first.Y - second.Y)
                if (dx <= TOLERANCE) == (dy <= TOLERANCE):
                    raise ValueError("The native model crop edges do not match the view axes.")
                points.append(first)
            if len(points) != 4:
                raise ValueError("The native model crop does not have four corners.")
            for point in points:
                matches = [corner for corner in expected if abs(corner[0] - point.X) <= TOLERANCE
                           and abs(corner[1] - point.Y) <= TOLERANCE]
                if len(matches) != 1:
                    raise ValueError("The native model crop does not match the reviewed crop.")
                expected.remove(matches[0])
            return sorted([point.X, point.Y] for point in points)
        finally:
            for loop in loops:
                loop.Dispose()
    finally:
        manager.Dispose()


def configure_view(document, view, spec, template, scale, phase, clip_mode):
    if not view.IsValidViewTemplate(template.Id):
        raise ValueError("The selected template is invalid for the created view.")
    view.ViewTemplateId = template.Id
    view.Name = spec["name"]
    document.Regenerate()
    check_scope(view)
    controlled = controlled_parameters(template)
    if not view.CropBoxActive:
        active = parameter(view, "VIEWER_CROP_REGION", DB.StorageType.Integer)
        require_writable(active, controlled)
        view.CropBoxActive = True
    if view.Scale != scale:
        require_writable(parameter(view, "VIEW_SCALE", DB.StorageType.Integer), controlled)
        view.Scale = scale
    current_phase = parameter(view, "VIEW_PHASE", DB.StorageType.ElementId)
    if current_phase.AsElementId().Value != phase.Id.Value:
        require_writable(current_phase, controlled)
        if not current_phase.Set(phase.Id):
            raise ValueError("Revit refused the reviewed view phase.")
    manager = view.GetCropRegionShapeManager()
    try:
        if manager.Split or manager.ShapeSet:
            raise ValueError("The view has a split or shaped crop.")
    finally:
        manager.Dispose()
    crop, lower, upper = frame_bounds(view, spec)
    if any(abs(a - b) > TOLERANCE for a, b in zip(
            (crop.Min.X, crop.Min.Y, crop.Max.X, crop.Max.Y), lower + upper)):
        crop.Min = DB.XYZ(lower[0], lower[1], crop.Min.Z)
        crop.Max = DB.XYZ(upper[0], upper[1], crop.Max.Z)
        view.CropBox = crop
    mode = parameter(view, "VIEWER_BOUND_FAR_CLIPPING", DB.StorageType.Integer)
    if mode.AsInteger() != clip_mode:
        require_writable(mode, controlled)
        if not mode.Set(clip_mode):
            raise ValueError("Revit refused the reviewed far clip mode.")
    document.Regenerate()
    offset = parameter(view, "VIEWER_BOUND_OFFSET_FAR", DB.StorageType.Double)
    if abs(offset.AsDouble() - spec["far_extent"]) > TOLERANCE:
        require_writable(offset, controlled)
        if not offset.Set(spec["far_extent"]):
            raise ValueError("Revit refused the reviewed far clip distance.")


def verify_view(view, marker, slot, spec, room, template, scale, phase, clip_mode):
    origin, look, right = reader.view_frame(spec)
    if (view.Name != spec["name"] or view.ViewTemplateId.Value != template.Id.Value
            or str(view.GetTypeId().Value) != spec["type"]["id"] or view.Scale != scale
            or not view.CropBoxActive or view.DesignOption is not None
            or view.ViewDirection.DistanceTo(look.Negate()) > TOLERANCE
            or view.RightDirection.DistanceTo(right) > TOLERANCE
            or view.UpDirection.DistanceTo(DB.XYZ.BasisZ) > TOLERANCE
            or abs(view.Origin.Subtract(origin).DotProduct(look)) > TOLERANCE
            or parameter(view, "VIEW_PHASE", DB.StorageType.ElementId).AsElementId().Value != phase.Id.Value):
        raise ValueError("The created view does not match the reviewed identity or view frame.")
    check_scope(view)
    mode = parameter(view, "VIEWER_BOUND_FAR_CLIPPING", DB.StorageType.Integer).AsInteger()
    offset = parameter(view, "VIEWER_BOUND_OFFSET_FAR", DB.StorageType.Double).AsDouble()
    if mode != clip_mode or math.isnan(offset) or math.isinf(offset) or abs(offset - spec["far_extent"]) > TOLERANCE:
        raise ValueError("The created view does not match the reviewed far clip.")
    _, lower, upper = frame_bounds(view, spec)
    result = {"name": view.Name, "view": reader.element_identity(view), "kind": spec["kind"],
              "room_id": spec["room_id"], "template": reader.element_identity(template, True),
              "scale": scale, "phase": reader.element_identity(phase), "far_clip_mode": mode,
              "far_extent": offset, "view_origin": reader.xyz(view.Origin),
              "direction": reader.xyz(view.ViewDirection), "crop": read_crop(view, lower, upper)}
    if marker is not None:
        if marker.CurrentViewCount != 1 or marker.GetViewId(slot).Value != view.Id.Value:
            raise ValueError("The created elevation is not alone in its reviewed marker slot.")
        result.update(marker=reader.element_identity(marker), slot=slot,
                      marker_point=reader.xyz(marker_position(marker, origin, room)))
    return result


def effect_ids(items):
    result = set()
    for index, item in enumerate(items):
        if index >= MAX_EFFECTS or item.Value <= 0 or str(item.Value) in result:
            raise ValueError("Native transaction effects exceed the trial scope.")
        result.add(str(item.Value))
    return result


def trial(document, args):
    if (document is None or document.IsFamilyDocument or document.IsWorkshared
            or document.IsReadOnly or document.IsModifiable
            or args.get("mode") != "rollback_trial" or args.get("disposable_fixture") is not True):
        raise ValueError("Use the rollback trial in an approved non-workshared disposable project.")
    preflight = args["room_view_preflight"]
    if args.get("approved_proposal_sha256") != preflight.get("proposal_sha256"):
        raise ValueError("Approve the exact room view proposal digest before trial.")
    checked = reader.native_preflight(document, args["room_views"], preflight)
    control = args["unrelated_control"]
    control_element = resolve(document, control)
    baseline = reader.element_identity(control_element, True)
    settings = args["fixture_settings"]
    scale, clip_mode = settings["scale"], settings["far_clip_mode"]
    if type(scale) is not int or not 1 <= scale <= 24000 or type(clip_mode) is not int or clip_mode not in (1, 2):
        raise ValueError("Use the fixture's verified scale and finite far clip mode.")
    rooms = {item["id"]: item for item in json.loads(preflight["snapshot_json"])["rooms"]}
    views = json.loads(preflight["proposal_json"])["views"]
    related = set()
    for data in rooms.values():
        related.update(data[key]["id"] for key in ("host_plan", "level", "phase"))
        related.update(source["id"] for source in data["finish_sources"])
        for family in data["families"].values():
            related.update(item["id"] for item in family.values())
    if control["id"] in rooms or control["id"] in related:
        raise ValueError("Choose an unrelated fixture control outside the reviewed room view scope.")

    name = "Archi Magic room view trial " + uuid4().hex
    status = DB.TransactionStatus
    group = DB.TransactionGroup(document, name)
    transaction = DB.Transaction(document, name)
    events, faults, measurements = [], [], []
    additions = set()
    failure, rolled_back, committed = None, False, False

    def record(_sender, event):
        try:
            if (len(events) >= 4 or not event.GetDocument().Equals(document)
                    or list(event.GetTransactionNames()) != [name]):
                raise ValueError("A native transaction event is outside this trial.")
            events.append({
                "operation": str(event.Operation),
                "added": effect_ids(event.GetAddedElementIds()),
                "modified": effect_ids(event.GetModifiedElementIds()),
                "deleted": effect_ids(event.GetDeletedElementIds()),
            })
        except Exception:
            if not faults:
                faults.append("Native transaction effects could not be tied to the trial.")

    application = document.Application
    application.DocumentChanged += record
    try:
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit did not start the room view trial.")
        created = []
        for spec in views:
            source = rooms[spec["room_id"]]
            host, room, phase = [resolve(document, item) for item in
                                 (source["host_plan"], source, source["phase"])]
            template = resolve(document, spec["template"])
            view, marker, slot = create_view(document, spec, host, room, scale, additions)
            configure_view(document, view, spec, template, scale, phase, clip_mode)
            created.append((view, marker, slot, spec, room, template, scale, phase, clip_mode))
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("Revit did not finish the provisional room view transaction.")
        committed = True
        if faults or events != [{"operation": "TransactionCommitted", "added": additions,
                                 "modified": set(), "deleted": set()}]:
            faults.append("Native transaction effects differ from the reviewed additions.")
            raise ValueError("The committed trial effects differ from the reviewed additions.")
        measurements = [verify_view(*item) for item in created]
    except Exception as exc:
        failure = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        if type(exc).__name__ == "RegenerationFailedException":
            faults.append("Revit could not regenerate the trial model.")
    finally:
        try:
            state = transaction.GetStatus()
            if state == status.Started:
                transaction.RollBack()
                state = transaction.GetStatus()
            if (state in (status.Committed, status.RolledBack, status.Uninitialized)
                    and group.GetStatus() == status.Started):
                rolled_back = group.RollBack() == status.RolledBack and group.GetStatus() == status.RolledBack
        except Exception:
            faults.append("Revit did not establish a terminal rollback.")
        finally:
            application.DocumentChanged -= record

    observed = additions.union(*(event["added"] for event in events))
    effects = [{key: sorted(value, key=int) if isinstance(value, set) else value
                for key, value in event.items()} for event in events]
    if not rolled_back or faults:
        return {"status": "transaction_unresolved", "reason": failure or "; ".join(faults), "effects": effects}
    try:
        if any(reader.native_element(document, item) is not None for item in observed):
            raise ValueError("A trial element remains after rollback.")
        if reader.element_identity(resolve(document, control), True) != baseline:
            raise ValueError("The unrelated fixture control changed during trial.")
        restored, _ = reader.read_room_view_snapshot(document, args["room_views"])
        if restored != json.loads(preflight["snapshot_json"]):
            raise ValueError("The original room view evidence differs after rollback.")
    except Exception as exc:
        return {"status": "transaction_unresolved", "reason": str(exc), "effects": effects}
    if committed and (len(events) != 2 or events[1] != {"operation": "TransactionGroupRolledBack",
                                                       "added": set(), "modified": set(), "deleted": additions}):
        return {"status": "transaction_unresolved", "reason": "Exact group rollback effects are missing.",
                "effects": effects}
    if not committed and any(event["operation"] == "TransactionCommitted" or event["added"] - additions
                             or event["modified"] or event["deleted"] - additions for event in events):
        return {"status": "transaction_unresolved", "reason": "A stopped trial reported unexpected effects.",
                "effects": effects}
    if failure is not None:
        return {"status": "stopped_and_rolled_back", "reason": failure, "effects": effects}
    receipt = {"status": "trial_rolled_back", "transaction": name, "effects": effects,
               "proposal_sha256": checked["proposal_sha256"], "views": measurements}
    payload = json.dumps(receipt, ensure_ascii=True, allow_nan=False)
    if len(payload) > reader.MAX_BYTES:
        return {"status": "stopped_and_rolled_back", "reason": "The trial receipt exceeds the byte limit."}
    return {"status": "trial_rolled_back", "trial_json": payload}


if "doc" in globals() and "inputs" in globals():
    try:
        result = trial(doc, inputs)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "reason": str(error)}
