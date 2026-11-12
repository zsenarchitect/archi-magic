"""Build a read-only placement proposal from a reviewed linked DWG."""

import json

from pyrevit.api import DB

from cad_layout_preview import classify_room_boundaries, fields, number, propose
from read_cad_layout_surroundings import scan_surroundings
from read_linked_cad_layout import EVIDENCE_LIMIT, MM_PER_FOOT, element_id, encoded, read_snapshot, sha256

REQUEST_LIMIT = 128 * 1024


def view_scope(document, request, level_id):
    view_id, phase_id = (element_id(request[key]) for key in ("view_id", "phase_id"))
    view = document.ActiveView
    if (not isinstance(view, DB.ViewPlan) or view.ViewType != DB.ViewType.FloorPlan
            or view.IsTemplate or not view.UniqueId or view.Id.Value != view_id.Value):
        raise ValueError("Activate the reviewed floor plan.")
    level = view.GenLevel
    if level is None or level.Id.Value != level_id.Value:
        raise ValueError("The reviewed floor plan must belong to the selected CAD layout level.")
    parameter = view.get_Parameter(DB.BuiltInParameter.VIEW_PHASE)
    if (parameter is None or parameter.StorageType != DB.StorageType.ElementId or not parameter.HasValue
            or parameter.AsElementId().Value != phase_id.Value):
        raise ValueError("The active floor plan phase does not match the reviewed phase.")
    phase = document.GetElement(phase_id)
    if not isinstance(phase, DB.Phase) or not phase.UniqueId:
        raise ValueError("Select an existing Revit phase.")
    if DB.DesignOption.GetActiveDesignOptionId(document).Value != DB.ElementId.InvalidElementId.Value:
        raise ValueError("Activate the main model before previewing CAD layout placement.")
    return {"view_id": request["view_id"], "view_uid": str(view.UniqueId),
            "view_version_guid": str(view.VersionGuid), "phase_id": request["phase_id"],
            "phase_uid": str(phase.UniqueId), "phase_version_guid": str(phase.VersionGuid)}


def placement_choices(proposal, request):
    wall_choices, door_choices = request["walls"], request["doors"]
    if not isinstance(wall_choices, dict) or set(wall_choices) != {wall["id"] for wall in proposal["walls"]}:
        raise ValueError("Supply height and room-bounding choices for every proposed wall.")
    if not isinstance(door_choices, dict) or set(door_choices) != {door["source_ids"][0] for door in proposal["doors"]}:
        raise ValueError("Supply hand and facing choices for every proposed door marker.")
    walls, doors = {}, {}
    for identifier, entry in wall_choices.items():
        fields(entry, "height_mm room_bounding")
        if type(entry["room_bounding"]) is not bool:
            raise ValueError("Choose whether each proposed wall bounds rooms.")
        height = number(entry["height_mm"], 0.1, 30000 * MM_PER_FOOT)
        walls[identifier] = {"height_mm": height, "room_bounding": entry["room_bounding"]}
    for identifier, entry in door_choices.items():
        fields(entry, "hand_flipped facing_flipped")
        if any(type(value) is not bool for value in entry.values()):
            raise ValueError("Supply explicit Boolean door hand and facing flip states.")
        doors[identifier] = dict(entry)
    return walls, doors


def read_preview(document, request):
    fields(request, "source view_id phase_id walls doors")
    encoded(request, REQUEST_LIMIT)
    if document is None or document.Application.VersionNumber != "2026":
        raise ValueError("Open the reviewed floor plan in Revit 2026.")
    source = request["source"]
    if not isinstance(source, dict) or "expected_evidence_sha256" not in source:
        raise ValueError("Supply the reviewed DWG request with its evidence SHA-256.")
    evidence = read_snapshot(document, source)
    view = view_scope(document, request, element_id(evidence["level_element_id"]))
    proposal = propose(json.loads(evidence["snapshot_json"]), json.loads(evidence["verified_mapping_json"]))
    result = {"kind": "revit-cad-layout-preview", "document": evidence["document"], "view": view,
              "source_evidence_sha256": evidence["evidence_sha256"], "level_id": evidence["level_element_id"],
              "proposal": proposal}
    if proposal["status"] == "proposed":
        walls, doors = placement_choices(proposal, request)
        proposal = classify_room_boundaries(proposal, {key: row["room_bounding"] for key, row in walls.items()})
        result.update(proposal=proposal, wall_choices=walls, door_choices=doors)
    status = "review_required" if proposal["unresolved"] else proposal["status"]
    result.update(status=status, model_write_approved=False)
    if status == "proposed":
        result["geometry_sha256"] = sha256(encoded(result, EVIDENCE_LIMIT))
        result["existing_model_scan"] = scan_surroundings(document, proposal, walls)
    return {"status": status, "preview_json": encoded(result, EVIDENCE_LIMIT)}


if "inputs" in globals():
    try:
        result = read_preview(doc, inputs)
    except (ValueError, KeyError, TypeError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not preview the reviewed CAD layout.",
                  "error_type": type(error).__name__}
