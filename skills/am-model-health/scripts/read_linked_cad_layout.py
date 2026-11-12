"""Read reviewed primitives from one Revit-linked DWG in internal-axis millimetres."""

import hashlib
import json
import re
from time import monotonic

from System import Int64
from pyrevit.api import DB

from cad_layout_preview import FRAME, INPUT_LIMIT, fields, label, number

MM_PER_FOOT = 304.8
MAX_GEOMETRY = 20000
MAX_DEPTH = 12
MAX_ISSUES = 500


def element_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
            or int(value) >= 2**63):
        raise ValueError("Use a positive decimal-string Revit ID.")
    return DB.ElementId(Int64(int(value)))


def encoded(data, limit):
    payload = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(payload) > limit:
        raise ValueError("The CAD geometry evidence exceeds its size limit.")
    return payload


def point_mm(transform, point):
    model = transform.OfPoint(point)
    return [number(float(axis) * MM_PER_FOOT, -1e7, 1e7) for axis in (model.X, model.Y, model.Z)]


def scope(request):
    fields(request, "link_id level_id segment_layers room_layers room_points_mm")
    layers = {}
    for key in ("segment_layers", "room_layers"):
        raw = request[key]
        if not isinstance(raw, list) or len(raw) > 128:
            raise ValueError("Supply up to 128 reviewed DWG layers.")
        layers[key] = {label(value) for value in raw}
        if len(layers[key]) != len(raw):
            raise ValueError("A reviewed layer appears more than once.")
    if (not any(layers.values()) or layers["segment_layers"] & layers["room_layers"]
            or sum(map(len, layers.values())) > 128):
        raise ValueError("Assign each reviewed DWG layer to one geometry role.")
    seeds = request["room_points_mm"]
    if not isinstance(seeds, dict) or len(seeds) > 32:
        raise ValueError("Supply a bounded map of reviewed room points.")
    for primitive, point in seeds.items():
        label(primitive)
        if not isinstance(point, list) or len(point) != 3:
            raise ValueError("Each reviewed room point needs X, Y and Z in millimetres.")
        for coordinate in point:
            number(coordinate, -1e7, 1e7)
    return layers, seeds


def linked_dwg(document, request):
    link = document.GetElement(element_id(request["link_id"]))
    if not isinstance(link, DB.ImportInstance) or not link.IsLinked or not link.UniqueId:
        raise ValueError("Select an existing linked DWG instance.")
    link_type = document.GetElement(link.GetTypeId())
    if link_type is None or not link_type.IsExternalFileReference():
        raise ValueError("The linked CAD type has no verifiable external file reference.")
    reference = link_type.GetExternalFileReference()
    if reference is None or str(reference.GetLinkedFileStatus()) != "Loaded":
        raise ValueError("The linked CAD file is not loaded.")
    path = reference.GetAbsolutePath() or reference.GetPath()
    name = DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(path) if path is not None else ""
    if not name or not name.lower().endswith(".dwg"):
        raise ValueError("The loaded linked file could not be identified as DWG.")
    return link


def read_geometry(document, root, layers, seeds):
    seen_layers, seen_seeds = set(), set()
    segments, loops, candidates, unresolved = [], [], [], []
    style_names = {}
    count = vertices = 0
    deadline = monotonic() + 10

    def warn(identifier, layer, message):
        if len(unresolved) >= MAX_ISSUES:
            raise ValueError("Too many unresolved CAD primitives. Narrow the reviewed scope.")
        unresolved.append({"id": identifier, "layer": layer, "message": message})

    def layer_name(item):
        style_id = item.GraphicsStyleId
        key = int(style_id.Value)
        if key not in style_names:
            style = document.GetElement(style_id) if key > 0 else None
            style_names[key] = (label(style.GraphicsStyleCategory.Name) if
                                isinstance(style, DB.GraphicsStyle) and style.GraphicsStyleCategory else None)
        return style_names[key]

    def walk(geometry, transform, path=()):
        nonlocal count, vertices
        if geometry is None or len(path) > MAX_DEPTH:
            raise ValueError("The linked DWG geometry is unavailable or exceeds the nesting limit.")
        for index, item in enumerate(geometry, 1):
            count += 1
            if count > MAX_GEOMETRY or monotonic() >= deadline:
                raise ValueError("The complete linked DWG geometry exceeds its read budget.")
            piece = (*path, index)
            identifier = "dwg-" + ".".join(map(str, piece))
            if isinstance(item, DB.GeometryInstance):
                # Symbol geometry is local to each nested instance. Compose exactly once.
                walk(item.GetSymbolGeometry(), transform.Multiply(item.Transform), piece)
                continue
            layer = layer_name(item)
            if layer is None:
                warn(identifier, None, "The primitive's DWG layer is unavailable.")
                continue
            if layer in layers["segment_layers"]:
                seen_layers.add(layer)
                if not isinstance(item, DB.Line) or not item.IsBound:
                    warn(identifier, layer, "The wall or door layer needs a bound native straight line.")
                    continue
                if len(segments) >= 400:
                    raise ValueError("The reviewed DWG scope exceeds 400 segments.")
                segments.append({"id": identifier, "layer": layer,
                                 "start": point_mm(transform, item.GetEndPoint(0)),
                                 "end": point_mm(transform, item.GetEndPoint(1))})
            elif layer in layers["room_layers"]:
                seen_layers.add(layer)
                if not isinstance(item, DB.PolyLine):
                    warn(identifier, layer, "The room layer needs a native closed polyline.")
                    continue
                points = item.GetCoordinates()
                if not 4 <= points.Count <= 65:
                    warn(identifier, layer, "A room polyline needs 3 to 64 vertices plus its closing point.")
                    continue
                positions = [point_mm(transform, point) for point in points]
                if positions[0] != positions[-1]:
                    warn(identifier, layer, "The room polyline has no explicit closing point.")
                    continue
                ring = positions[:-1]
                # A reviewed point belongs to this contour, not just its traversal index.
                fingerprint = hashlib.sha256(encoded([layer, ring], INPUT_LIMIT).encode("ascii")).hexdigest()
                identifier += "-" + fingerprint
                vertices += len(ring)
                if vertices > 512 or len(loops) + len(candidates) >= 32:
                    raise ValueError("The reviewed DWG scope exceeds the room or vertex limit.")
                candidate = {"id": identifier, "layer": layer, "closed": True, "vertices": ring}
                if identifier in seeds:
                    seen_seeds.add(identifier)
                    loops.append({**candidate, "interior_point": seeds[identifier]})
                else:
                    candidates.append(candidate)
                    warn(identifier, layer, "Choose and review an interior point before proposing this room.")

    walk(root, DB.Transform.Identity)
    for layer in sorted(set.union(*layers.values()) - seen_layers):
        warn(None, layer, "No geometry with this exact reviewed layer was found in the link.")
    if set(seeds) != seen_seeds:
        raise ValueError("A reviewed room point did not match a closed polyline in the current DWG read.")
    return segments, loops, candidates, unresolved


def read_snapshot(document, request):
    if document is None or document.IsFamilyDocument or not document.PathName or document.ProjectInformation is None:
        raise ValueError("Open the saved project containing the linked DWG.")
    layers, seeds = scope(request)
    link = linked_dwg(document, request)
    level = document.GetElement(element_id(request["level_id"]))
    if not isinstance(level, DB.Level) or not level.UniqueId:
        raise ValueError("Select the existing Revit level for the CAD layout.")
    options = DB.Options()
    try:
        options.ComputeReferences = False
        options.IncludeNonVisibleObjects = False
        geometry = link.get_Geometry(options)
        segments, loops, candidates, unresolved = read_geometry(document, geometry, layers, seeds)
    finally:
        options.Dispose()
    snapshot = {"coordinate_frame": FRAME, "link_id": str(link.UniqueId), "level_id": str(level.UniqueId),
                "level_mm": number(float(level.ProjectElevation) * MM_PER_FOOT, -1e7, 1e7),
                "segments": segments, "room_loops": loops}
    document_key = {"project_uid": str(document.ProjectInformation.UniqueId),
                    "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}
    evidence = {"status": "review_required" if unresolved else "ok", "document": document_key,
                "link_element_id": request["link_id"], "level_element_id": request["level_id"],
                "snapshot_json": encoded(snapshot, INPUT_LIMIT), "room_candidates": candidates,
                "unresolved": unresolved}
    encoded(evidence, 2 * INPUT_LIMIT)
    return evidence


if "inputs" in globals():
    try:
        result = read_snapshot(doc, inputs)
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not verify the linked DWG geometry.",
                  "error_type": type(error).__name__}
