"""Prepare and measure floor reconstruction inside an already reversible split trial."""

import math

from pyrevit.api import DB

import element_split_preview as planner
import read_element_split_evidence as reader
from probe_floor_split_cascade import eid, floor_members


class UnexpectedFloorDeletion(ValueError):
    """The native deletion set could not be reconciled with the reviewed cascade."""


def fixture_ids(document, budget):
    collector = DB.FilteredElementCollector(document)
    iterator = None
    try:
        iterator = collector.WhereElementIsNotElementType().GetElementIdIterator()
        ids = set()
        while iterator.MoveNext():
            budget.visit("dependencies")
            ids.add(planner.identifier(str(iterator.Current.Value)))
        return ids
    finally:
        if iterator is not None:
            iterator.Dispose()
        collector.Dispose()


def floor_volume(element, geometry, budget):
    tolerance = reader.TOLERANCE_FT
    references = iter(DB.HostObjectUtils.GetBottomFaces(element))
    reference = next(references, None)
    if reference is None or next(references, None) is not None:
        raise ValueError("Use one independently measurable floor bottom face.")
    face = element.GetGeometryObjectFromReference(reference)
    if not isinstance(face, DB.PlanarFace) or math.dist(reader.xyz(face.FaceNormal), [0, 0, -1]) > tolerance:
        raise ValueError("The floor bottom face is not horizontal.")
    bottom = reader.horizontal_loop(face.GetEdgesAsCurveLoops(), budget)
    if not reader.same_floor_boundary(bottom, geometry["boundary_xy_ft"]):
        raise ValueError("The floor bottom and top boundaries differ.")
    thickness = float(geometry["elevation_ft"]) - bottom[0][2]
    points = geometry["boundary_xy_ft"]
    area = abs(float(planner.twice_area(points))) / 2
    options = DB.Options()
    try:
        solids = []
        for item in element.get_Geometry(options):
            budget.visit()
            if isinstance(item, DB.Solid) and item.Volume > 0:
                solids.append(float(item.Volume))
        if (thickness <= tolerance or len(solids) != 1
                or not math.isclose(solids[0], area * thickness, rel_tol=1e-6, abs_tol=tolerance)):
            raise ValueError("The native floor solid does not match the measured prism.")
    finally:
        options.Dispose()
    return {"thickness_ft": thickness, "solid_volume_cuft": solids[0]}


def prepare(document, receipt, snapshot, inventory_sha, snapshot_sha, element):
    planner.fields(receipt, {"kind", "host_identity", "source", "sketch", "snapshot_sha256", "inventory_sha256", "deleted"})
    if (receipt["kind"] != "element-split-floor-cascade" or receipt["host_identity"] != snapshot["host_identity"]
            or receipt["snapshot_sha256"] != snapshot_sha or receipt["inventory_sha256"] != inventory_sha):
        raise ValueError("The reviewed cascade does not match the live floor evidence.")
    scope = snapshot["elements"][0]
    source, sketch, allowed = floor_members(document, {"id": scope["id"], "uid": scope["uid"]})
    if receipt["source"] != source or receipt["sketch"] != sketch:
        raise ValueError("The reviewed floor or sketch identity changed.")
    approved = {}
    for row in planner.bounded_list(receipt["deleted"], 2, reader.MAX_DEPENDENCIES + 2, "reviewed deletions"):
        planner.fields(row, {"id", "uid"})
        identifier = planner.identifier(row["id"])
        if identifier in approved or allowed.get(identifier) != row:
            raise ValueError("A reviewed deletion is outside the owned floor and sketch elements.")
        approved[identifier] = row
    if {source["id"], sketch["id"]} - approved.keys():
        raise ValueError("The reviewed cascade must include the original floor and sketch.")

    level = document.GetElement(element.LevelId)
    if not isinstance(level, DB.Level):
        raise ValueError("The original floor level could not be identified.")
    offset = reader.parameter_value(element, "FLOOR_HEIGHTABOVELEVEL_PARAM", DB.StorageType.Double, "AsDouble")
    structural = reader.parameter_value(element, "FLOOR_PARAM_IS_STRUCTURAL", DB.StorageType.Integer, "AsInteger")
    if structural not in (0, 1):
        raise ValueError("The original floor structural flag is unreadable.")
    budget = reader.ReadBudget()
    return {"source": source, "sketch": sketch, "allowed": allowed, "approved": approved,
            "level": reader.identity(level), "offset_ft": reader.measure(offset), "structural": bool(structural),
            "inventory": fixture_ids(document, budget), "solid": floor_volume(element, scope["geometry"], budget)}


def profile(geometry, z):
    from System.Collections.Generic import List

    polygon = geometry["boundary_xy_ft"]
    loop = DB.CurveLoop()
    for a, b in planner.edges(polygon):
        loop.Append(DB.Line.CreateBound(DB.XYZ(float(a[0]), float(a[1]), z), DB.XYZ(float(b[0]), float(b[1]), z)))
    profiles = List[DB.CurveLoop]()
    profiles.Add(loop)
    if not DB.BoundaryValidation.IsValidHorizontalBoundary(profiles):
        raise ValueError("Revit rejected a reviewed floor boundary.")
    return profiles


def replace(document, floor, basis, expected, created, budget):
    level = reader.selected(document, basis["level"])
    level_id, type_id = level.Id, floor.GetTypeId()
    profiles = []
    for piece in expected["pieces"]:
        budget.visit()
        profiles.append(profile(piece, level.ProjectElevation))
    deleted = set()
    try:
        for raw in document.Delete(floor.Id):
            if len(deleted) >= reader.MAX_DEPENDENCIES + 2:
                raise ValueError("The native deletion list exceeds its budget.")
            value = planner.identifier(str(raw.Value))
            if value in deleted:
                raise ValueError("Revit repeated an ID in the deletion list.")
            deleted.add(value)
        if deleted != basis["approved"].keys() or any(document.GetElement(eid(value)) is not None for value in deleted):
            raise ValueError("The native deletion list differs from the separately reviewed cascade.")
    except Exception as error:
        if type(error).__name__ == "RegenerationFailedException":
            raise
        raise UnexpectedFloorDeletion(str(error)) from error

    pieces = []
    for loops in profiles:
        budget.visit()
        piece = DB.Floor.Create(document, loops, type_id, level_id, basis["structural"], None, 0.0)
        identifier = planner.identifier(str(piece.Id.Value))
        if identifier in created or identifier in basis["inventory"]:
            raise ValueError("Revit reused a preexisting ID for a replacement floor.")
        created.add(identifier)
        offset = piece.get_Parameter(DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        if offset is None or offset.StorageType != DB.StorageType.Double or offset.IsReadOnly or not offset.Set(basis["offset_ft"]):
            raise ValueError("The original floor offset could not be restored on a piece.")
        pieces.append(piece)
    document.Regenerate()
    budget.visit()
    return pieces


def effects(document, pieces, basis):
    owned = set()
    budget = reader.ReadBudget()
    for piece in pieces:
        source, sketch, members = floor_members(document, piece["result"])
        if source["id"] in basis["inventory"] or sketch["id"] in basis["inventory"]:
            raise ValueError("A replacement floor or sketch reused an existing element.")
        owned.update(members)
    current = fixture_ids(document, budget)
    if current - basis["inventory"] != owned - basis["inventory"] or basis["inventory"] - current != basis["approved"].keys():
        raise ValueError("The trial created or deleted model elements outside the reviewed floor and owned sketches.")
    return current - basis["inventory"]


def verify_solids(document, requests, measured, basis):
    budget = reader.ReadBudget()
    geometry = {piece["id"]: piece["geometry"] for piece in measured["pieces"]}
    result = {}
    for row in requests:
        element = reader.selected(document, row["result"])
        if any(True for _ in DB.JoinGeometryUtils.GetJoinedElements(document, element)):
            raise ValueError("A replacement floor acquired a geometry join.")
        item = floor_volume(element, geometry[row["result"]["id"]], budget)
        if abs(item["thickness_ft"] - basis["solid"]["thickness_ft"]) > reader.TOLERANCE_FT:
            raise ValueError("A replacement floor changed thickness.")
        result[row["result"]["id"]] = item
    if not math.isclose(sum(item["solid_volume_cuft"] for item in result.values()),
                        basis["solid"]["solid_volume_cuft"], rel_tol=1e-6, abs_tol=reader.TOLERANCE_FT):
        raise ValueError("The replacement pieces do not conserve source floor volume.")
    return result


def verify_restored(document, basis, geometry):
    budget = reader.ReadBudget()
    floor = reader.selected(document, basis["source"])
    if (fixture_ids(document, budget) != basis["inventory"]
            or floor_members(document, basis["source"]) != (basis["source"], basis["sketch"], basis["allowed"])
            or floor_volume(floor, geometry, budget) != basis["solid"]):
        raise ValueError("The floor, sketch members, solid, or fixture inventory differs after rollback.")
