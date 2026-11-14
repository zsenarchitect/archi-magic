"""Read the native model state required to review level alignment in Revit 2026."""

import hashlib
import json

from pyrevit.api import DB
from System import Int64

from level_alignment_preview import OPERATIONS, POSITION_TOLERANCE, element_id, finite, uid


def identity(document):
    if document is None or document.IsFamilyDocument or document.IsLinked or not document.PathName:
        raise ValueError("Open a saved host project before reading alignment inputs.")
    project_uid = str(document.ProjectInformation.UniqueId)
    if not uid(project_uid):
        raise ValueError("The saved project has no stable identity.")
    return {
        "project_uid": project_uid,
        "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest(),
    }


def key(element_id_value):
    return str(element_id_value.Value)


def get(document, id_value):
    return document.GetElement(DB.ElementId(Int64(int(id_value))))


def coords(point):
    result = [float(point.X), float(point.Y), float(point.Z)]
    if not all(map(finite, result)):
        raise ValueError("Revit returned an invalid location.")
    return result


def frame_evidence(document):
    base = DB.BasePoint.GetProjectBasePoint(document)
    location_uid = str(document.ActiveProjectLocation.UniqueId)
    if base is None or not uid(str(base.UniqueId)) or not uid(location_uid):
        raise ValueError("The project base point could not be read.")
    return {
        "project_base_uid": str(base.UniqueId),
        "project_base_point_ft": coords(base.Position),
        "project_location_uid": location_uid,
    }


def elevation(level, frame):
    native = float(level.ProjectElevation)
    converted = native + frame["project_base_point_ft"][2]
    if not finite(native) or not finite(converted):
        raise ValueError("A level has an invalid project elevation.")
    return converted


def frame_matches(document, evidence, project, frame):
    if (not isinstance(evidence, dict) or evidence.get("project") != project
            or evidence.get("frame") != frame):
        return False
    anchors = evidence.get("anchors")
    if not isinstance(anchors, list) or len(anchors) != 2:
        return False
    measured = []
    for anchor in anchors:
        if (not isinstance(anchor, dict) or not element_id(anchor.get("id"))
                or not uid(anchor.get("uid")) or not finite(anchor.get("internal_z_ft"))):
            return False
        level = get(document, anchor["id"])
        if (not isinstance(level, DB.Level) or str(level.UniqueId) != anchor["uid"]
                or abs(elevation(level, frame) - anchor["internal_z_ft"]) > POSITION_TOLERANCE):
            return False
        measured.append(anchor["internal_z_ft"])
    return abs(measured[1] - measured[0]) > POSITION_TOLERANCE


def ownership(document, element):
    if document.IsReadOnly:
        return False, None
    if not document.IsWorkshared:
        return True, None
    checkout = str(DB.WorksharingUtils.GetCheckoutStatus(document, element.Id))
    central = str(DB.WorksharingUtils.GetModelUpdatesStatus(document, element.Id))
    return (checkout in ("OwnedByCurrentUser", "NotOwned")
            and central in ("CurrentWithCentral", "NotYetInCentral")), [checkout, central]


def read_offset(document, element, level, frame):
    level_parameter = element.get_Parameter(DB.BuiltInParameter.FAMILY_LEVEL_PARAM)
    offset_parameter = element.get_Parameter(DB.BuiltInParameter.INSTANCE_ELEVATION_PARAM)
    if (level is None or level_parameter is None or offset_parameter is None
            or not level_parameter.HasValue or not offset_parameter.HasValue
            or level_parameter.StorageType != DB.StorageType.ElementId
            or offset_parameter.StorageType != DB.StorageType.Double
            or not offset_parameter.Definition.GetDataType().Equals(DB.SpecTypeId.Length)):
        return {}
    reported_level = document.GetElement(level_parameter.AsElementId())
    offset = float(offset_parameter.AsDouble())
    if not isinstance(reported_level, DB.Level) or not finite(offset):
        return {}
    return {
        "level_parameter_uid": str(reported_level.UniqueId),
        "level_z_ft": elevation(level, frame),
        "offset_ft": offset,
        "level_writable": not level_parameter.IsReadOnly,
        "offset_writable": not offset_parameter.IsReadOnly,
        "offset_contract": {
            "level_parameter_id": key(level_parameter.Id),
            "offset_parameter_id": key(offset_parameter.Id),
            "length_spec": str(DB.SpecTypeId.Length.TypeId),
            "binding": "instance",
            "level_storage": "ElementId",
            "offset_storage": "Double",
        },
    }


def instance(document, id_value, target, frame):
    result = {"id": id_value, "model_point": False, "supported": False, "editable": False}
    element = get(document, id_value)
    if (not isinstance(element, DB.FamilyInstance) or not document.Equals(element.Document)
            or element.ViewSpecific is not False):
        return result
    location = element.Location
    if not isinstance(location, DB.LocationPoint):
        return result
    rotation = float(location.Rotation)
    if not finite(rotation):
        return result

    family = element.Symbol.Family
    level = document.GetElement(element.LevelId)
    if not isinstance(level, DB.Level):
        level = None
    host = element.Host
    group_id = key(element.GroupId)
    one_level = (family.FamilyPlacementType == DB.FamilyPlacementType.OneLevelBased
                 and host is None and element.HostFace is None and group_id == "-1")
    supported = one_level and element.Pinned is False
    result.update(
        model_point=True,
        supported=supported,
        uid=str(element.UniqueId),
        type_uid=str(element.Symbol.UniqueId),
        point=coords(location.Point),
        rotation_rad=rotation,
        level_uid=str(level.UniqueId) if level else None,
        level_id=key(level.Id) if level else None,
        host_id=key(host.Id) if host is not None else None,
        group_id=group_id,
        pinned=element.Pinned,
        placement_type=str(family.FamilyPlacementType),
    )
    if target and supported:
        result["editable"], result["ownership"] = ownership(document, element)
    if frame is not None and one_level:
        result.update(read_offset(document, element, level, frame))
    return result


def levels(document, frame):
    result = []
    for level in DB.FilteredElementCollector(document).OfClass(DB.Level):
        if len(result) == 500:
            raise ValueError("The eligible level inventory exceeds 500 levels.")
        result.append({"id": key(level.Id), "uid": str(level.UniqueId), "elevation_ft": elevation(level, frame)})
    if not result:
        raise ValueError("No eligible levels were found.")
    return sorted(result, key=lambda level: int(level["id"]))


def read_snapshot(document, request):
    project = identity(document)
    operation, ids = request.get("operation"), request.get("target_ids")
    reference = request.get("reference_id")
    if (set(request) - {"operation", "target_ids", "reference_id", "verified_frame"}
            or operation not in OPERATIONS or not isinstance(ids, list) or not 1 <= len(ids) <= 30
            or not all(map(element_id, ids)) or len(set(ids)) != len(ids)):
        raise ValueError("Supply an alignment operation and up to 30 distinct decimal target IDs.")
    if operation == "nearest_level":
        if reference is not None:
            raise ValueError("Nearest-level matching does not use a reference element.")
    elif not element_id(reference) or reference in ids:
        raise ValueError("Supply one reference ID outside the target list.")

    frame = frame_evidence(document) if operation != "rotation" else None
    application = document.Application
    revit = {"version": str(application.VersionNumber), "build": str(application.VersionBuild)}
    if not all(map(uid, revit.values())):
        raise ValueError("Revit did not report a version and build.")
    snapshot = {
        "kind": "level-alignment-snapshot",
        "revit": revit,
        "version": 1,
        "project": project,
        "operation": operation,
        "targets": [instance(document, item, True, frame) for item in ids],
    }
    if reference is not None:
        snapshot["reference"] = instance(document, reference, False, frame)
    if frame is not None:
        snapshot["frame"] = frame
        evidence = request.get("verified_frame")
        snapshot["frame_verified"] = frame_matches(document, evidence, project, frame)
        if snapshot["frame_verified"]:
            snapshot["frame_evidence"] = evidence
    if operation == "nearest_level":
        snapshot["levels"] = levels(document, frame)
        snapshot["levels_complete"] = True
    return snapshot


def main(document, args):
    try:
        snapshot = read_snapshot(document, args["request"])
        return {
            "status": "ok",
            "snapshot_json": json.dumps(snapshot, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")),
        }
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        return {"status": "stopped", "message": str(error)}
    except Exception as error:
        return {"status": "stopped", "message": "Revit could not read the alignment inputs.", "error_type": type(error).__name__}


if "doc" in globals() and "inputs" in globals():
    result = main(doc, inputs)
