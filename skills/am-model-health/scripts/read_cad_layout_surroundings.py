"""Inventory existing model elements near proposed CAD placement geometry."""

import math
from time import monotonic

from pyrevit.api import DB

from read_linked_cad_layout import MM_PER_FOOT

CATEGORIES = ("OST_Walls", "OST_Doors", "OST_Rooms", "OST_RoomSeparationLines",
              "OST_Columns", "OST_StructuralColumns", "OST_Floors", "OST_Ceilings",
              "OST_Roofs", "OST_Curtain_Systems", "OST_GenericModel", "OST_RvtLinks")
MAX_VISITED = 5000
MAX_CANDIDATES = 500
MAX_ASSOCIATIONS = 5000
MAX_SECONDS = 15


def bounds(points, bottom, top, padding):
    x, y = zip(*points)
    return ([min(x) - padding, min(y) - padding, bottom - padding],
            [max(x) + padding, max(y) + padding, top + padding])


def proposal_bounds(proposal, wall_choices):
    level = proposal["level_mm"]
    tolerance = proposal["tolerance_mm"]
    volumes = {}
    for wall in proposal["walls"]:
        key = wall["id"]
        top = level + wall_choices[key]["height_mm"]
        volumes[key] = bounds(wall["axis"], level, top, wall["width_mm"] / 2 + tolerance)
    for door in proposal["doors"]:
        span_start, span_end = door["span_mm"]
        top = level + wall_choices[door["host"]]["height_mm"]
        volumes["door:" + door["source_ids"][0]] = bounds(
            [door["location"]], level, top, (span_end - span_start) / 2 + tolerance)
    for room in proposal["rooms"]:
        volumes["room:" + room["source_ids"][0]] = bounds(room["boundary"], level, level, tolerance)
    return volumes


def element_bounds(element):
    # Element.BoundingBox already uses the host model axes, including for link instances.
    box = element.get_BoundingBox(None)
    if box is None:
        return None
    try:
        if not box.Enabled:
            return None
        low, high = ([float(point.X) * MM_PER_FOOT, float(point.Y) * MM_PER_FOOT,
                      float(point.Z) * MM_PER_FOOT] for point in (box.Min, box.Max))
        if not all(math.isfinite(a) and math.isfinite(b) and a <= b for a, b in zip(low, high)):
            return None
        return low, high
    finally:
        box.Dispose()


def overlaps(left, right):
    return all(a <= d and c <= b for a, b, c, d in zip(*left, *right))


def scan_surroundings(document, proposal, wall_choices):
    volumes = proposal_bounds(proposal, wall_choices)
    candidates, unavailable = [], []
    visited = unplaced_rooms = associations = 0
    limit = None
    deadline = monotonic() + MAX_SECONDS
    for category in CATEGORIES:
        if monotonic() >= deadline:
            limit = "time"
            break
        collector = DB.FilteredElementCollector(document)
        try:
            elements = collector.OfCategory(getattr(DB.BuiltInCategory, category)).WhereElementIsNotElementType()
            for element in elements:
                if visited >= MAX_VISITED or monotonic() >= deadline:
                    limit = "elements" if visited >= MAX_VISITED else "time"
                    break
                visited += 1
                identifier = str(element.Id.Value)
                try:
                    if category == "OST_Rooms" and element.Location is None:
                        unplaced_rooms += 1
                        continue
                    box = element_bounds(element)
                    hits = sorted(key for key, volume in volumes.items() if overlaps(volume, box)) if box else []
                    if box is not None and not hits:
                        continue
                    if not element.UniqueId:
                        raise ValueError("Missing element identity.")
                    if len(candidates) + len(unavailable) >= MAX_CANDIDATES:
                        limit = "evidence"
                        break
                    if box is None:
                        unavailable.append({"id": identifier, "category": category, "reason": "no_model_bounds"})
                        continue
                    associations += len(hits)
                    if associations > MAX_ASSOCIATIONS:
                        limit = "associations"
                        break
                    candidates.append({"id": identifier, "uid": str(element.UniqueId), "category": category,
                                       "version_guid": str(element.VersionGuid),
                                       "created_phase_id": str(element.CreatedPhaseId.Value),
                                       "demolished_phase_id": str(element.DemolishedPhaseId.Value),
                                       "bounds_mm": box, "near": hits})
                except Exception as error:
                    if len(candidates) + len(unavailable) >= MAX_CANDIDATES:
                        limit = "evidence"
                        break
                    unavailable.append({"id": identifier, "category": category, "reason": type(error).__name__})
        finally:
            collector.Dispose()
        if limit:
            break
    if limit is None and monotonic() >= deadline:
        limit = "time"
    return {"status": "incomplete" if limit or unavailable else "candidates" if candidates else "none_found",
            "categories": CATEGORIES, "visited": visited, "unplaced_rooms": unplaced_rooms, "limit": limit,
            "candidates": sorted(candidates, key=lambda row: int(row["id"])),
            "unavailable": sorted(unavailable, key=lambda row: int(row["id"]))}
