"""Measure exact reuse placement in a disposable Revit project and always roll it back."""

import math

from family_library_discovery import manifest_digest
from family_library_index import select_for_placement
from family_placement_preview import build_preview
from read_family_placement import eid, read_snapshot

TRANSACTION_NAME = "Trial family type reuse"
MAX_ELEMENTS = 10_000
POSITION_TOLERANCE_FT = 1e-6
ROTATION_TOLERANCE_DEG = 1e-5


def inventory(document, DB):
    elements = {}
    for collector in (DB.FilteredElementCollector(document).WhereElementIsElementType(),
                      DB.FilteredElementCollector(document).WhereElementIsNotElementType()):
        for element in collector:
            key = eid(element)
            if key in elements or len(elements) >= MAX_ELEMENTS:
                raise ValueError("The disposable project exceeds the complete inventory limit.")
            elements[key] = [str(element.UniqueId), str(element.VersionGuid)]
    return elements


def measure(DB, instance, review):
    location = instance.Location
    if (not isinstance(location, DB.LocationPoint) or instance.Host is not None
            or eid(instance.Symbol) != review["symbol"]["id"]
            or str(instance.Symbol.UniqueId) != review["symbol"]["uid"]
            or str(instance.LevelId.Value) != review["level"]["id"]):
        raise ValueError("The trial instance does not have the reviewed point, symbol, and level.")
    point = [float(location.Point.X), float(location.Point.Y), float(location.Point.Z)]
    angle = math.degrees(location.Rotation) % 360
    delta = (angle - review["rotation_deg"] + 180) % 360 - 180
    if (not all(math.isfinite(value) for value in point + [angle])
            or any(abs(actual - expected) > POSITION_TOLERANCE_FT
                   for actual, expected in zip(point, review["absolute_xyz_ft"]))
            or abs(delta) > ROTATION_TOLERANCE_DEG):
        raise ValueError("The measured absolute placement or rotation differs from the review.")
    return {"id": eid(instance), "uid": str(instance.UniqueId), "xyz_ft": point,
            "rotation_deg": angle, "level_id": review["level"]["id"],
            "symbol_id": review["symbol"]["id"]}


def validate_effects(before, after, event, placed, ownership):
    expected_modified = set(ownership)
    new_ids = set(after) - set(before)
    removed = set(before) - set(after)
    changed = {key for key in set(before) & set(after) if before[key] != after[key]}
    if (new_ids != {placed} or removed or changed - expected_modified
            or set(event["added"]) != {placed} or event["deleted"]
            or set(event["modified"]) != expected_modified):
        raise ValueError("Revit reported effects outside the reviewed instance and activation.")


def rollback(transaction, group, status):
    current = transaction.GetStatus()
    if current == status.Started:
        if transaction.RollBack() != status.RolledBack or transaction.GetStatus() != status.RolledBack:
            return False
    elif current not in (status.Uninitialized, status.Committed, status.RolledBack):
        return False
    group_state = group.GetStatus()
    if group_state == status.Started:
        return group.RollBack() == status.RolledBack and group.GetStatus() == status.RolledBack
    return group_state == status.Uninitialized and current == status.Uninitialized


def get_element(document, DB, identifier):
    return document.GetElement(DB.ElementId(int(identifier)))


def trial(document, DB, args):
    if (set(args) != {"disposable_fixture", "index", "preview", "preview_sha256"}
            or args["disposable_fixture"] is not True or document.IsModifiable):
        raise ValueError("Use the rollback probe in a saved disposable project with no open transaction.")
    preview = args["preview"]
    review = preview["review"]
    if (manifest_digest(preview) != args["preview_sha256"]
            or preview["review_sha256"] != manifest_digest(review)
            or review["action"] != "reuse" or review["placement_type"] != "OneLevelBased"):
        raise ValueError("Use the reviewed preview for exact one-level project type reuse.")
    selected = review["selection"]
    current = select_for_placement(args["index"], selected["generation"], selected["source"]["path"], selected["type"])
    if current != selected:
        raise ValueError("The published selection or source changed after review.")
    request = {"selection": current, "level_id": review["level"]["id"]}
    before = read_snapshot(document, DB, request)
    fresh = build_preview(current, before, review["absolute_xyz_ft"], review["rotation_deg"])
    if fresh["review"] != review:
        raise ValueError("The saved project, symbol, level, or ownership scope changed after review.")
    original = inventory(document, DB)
    events = []
    closing = False
    invalid = False

    def record(sender, event):
        nonlocal invalid
        if closing:
            return
        try:
            if (events or not event.GetDocument().Equals(document)
                    or str(event.Operation) != "TransactionCommitted"
                    or list(event.GetTransactionNames()) != [TRANSACTION_NAME]):
                invalid = True
                return
            effects = {"added": [str(item.Value) for item in event.GetAddedElementIds()],
                       "deleted": [str(item.Value) for item in event.GetDeletedElementIds()],
                       "modified": [str(item.Value) for item in event.GetModifiedElementIds()]}
            events.append(effects)
        except Exception:
            invalid = True

    status = DB.TransactionStatus
    group = DB.TransactionGroup(document, TRANSACTION_NAME)
    transaction = DB.Transaction(document, TRANSACTION_NAME)
    application = document.Application
    failure = None
    regeneration_failed = False
    placement = None
    restored = False
    application.DocumentChanged += record
    try:
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit could not start the placement trial.")
        symbol = get_element(document, DB, review["symbol"]["id"])
        level = get_element(document, DB, review["level"]["id"])
        if str(symbol.UniqueId) != review["symbol"]["uid"] or str(level.UniqueId) != review["level"]["uid"]:
            raise ValueError("The exact symbol or level is unavailable.")
        if not symbol.IsActive:
            symbol.Activate()
            document.Regenerate()
        x, y, _ = review["absolute_xyz_ft"]
        insertion = DB.XYZ(x, y, review["level_offset_ft"])
        instance = document.Create.NewFamilyInstance(insertion, symbol, level, DB.Structure.StructuralType.NonStructural)
        if instance is None:
            raise ValueError("Revit did not return a trial instance.")
        document.Regenerate()
        location = instance.Location
        if not isinstance(location, DB.LocationPoint):
            raise ValueError("The trial instance does not have a point location.")
        angle = (math.radians(review["rotation_deg"]) - location.Rotation + math.pi) % (2 * math.pi) - math.pi
        if abs(angle) > 1e-10:
            axis = DB.Line.CreateBound(location.Point, location.Point + DB.XYZ.BasisZ)
            DB.ElementTransformUtils.RotateElement(document, instance.Id, axis, angle)
        document.Regenerate()
        measured = measure(DB, instance, review)
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("Revit did not commit the provisional trial.")
        if invalid or len(events) != 1:
            raise ValueError("Revit did not return one complete provisional transaction record.")
        instance = get_element(document, DB, measured["id"])
        placement = measure(DB, instance, review)
        if placement["uid"] != measured["uid"]:
            raise ValueError("The trial instance identity changed at commit.")
        after = read_snapshot(document, DB, request, require_unmodified=False)
        expected = dict(before)
        expected["exact_symbols"] = [dict(review["symbol"], active=True)]
        if after != expected:
            raise ValueError("The project type or level changed beyond the reviewed activation.")
        validate_effects(original, inventory(document, DB), events[0], placement["id"], review["ownership_ids"])
    except Exception as error:
        regeneration_failed = type(error).__name__ == "RegenerationFailedException"
        failure = str(error) if isinstance(error, ValueError) else type(error).__name__
    finally:
        closing = True
        try:
            restored = rollback(transaction, group, status)
        except Exception:
            restored = False
        finally:
            try:
                application.DocumentChanged -= record
            except Exception:
                restored = False
            for scope in (transaction, group):
                try:
                    scope.Dispose()
                except Exception:
                    restored = False

    if not restored or regeneration_failed:
        return {"status": "transaction_unresolved", "reason": failure or "Rollback was not confirmed."}
    try:
        if read_snapshot(document, DB, request, require_unmodified=False) != before or inventory(document, DB) != original:
            return {"status": "transaction_unresolved", "reason": "The baseline changed after rollback."}
    except Exception as error:
        return {"status": "transaction_unresolved", "reason": type(error).__name__}
    if failure:
        return {"status": "stopped", "reason": failure, "rollback_verified": True}
    return {"status": "trial_rolled_back", "preview_sha256": args["preview_sha256"],
            "placement": placement, "effects": events[0], "rollback_verified": True}


try:
    active_project, query_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        report = trial(active_project, DB, query_inputs)
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
        report = {"status": "stopped", "reason": str(error)}
    except Exception as error:
        report = {"status": "transaction_unresolved", "reason": type(error).__name__}
    result = report
