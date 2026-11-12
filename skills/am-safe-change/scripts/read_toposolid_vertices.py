# -*- coding: utf-8 -*-
"""Read the existing shape vertices of one native Toposolid through run_query."""

import hashlib
import json
import math
import re

from System import Int64
from System.Diagnostics import Stopwatch
from pyrevit.api import DB


MAX_VERTICES = 2000
MAX_READ_MS = 5000
MAX_PAYLOAD_BYTES = 256 * 1024


def read_vertices(document, args):
    if document is None or document.IsFamilyDocument or not document.PathName:
        raise ValueError("Open the saved Revit project containing the Toposolid.")
    if str(document.Application.VersionNumber) != "2026":
        raise ValueError("Use Revit 2026 for this Toposolid inventory.")
    if not isinstance(args, dict) or set(args) != {"toposolid_id"}:
        raise ValueError("Supply only inputs.toposolid_id.")
    value = args["toposolid_id"]
    if (not isinstance(value, (str, type(u""))) or not re.match(r"[1-9][0-9]{0,18}\Z", value)
            or int(value) >= 2 ** 63):
        raise ValueError("Supply a positive decimal Toposolid ID as text.")
    clock = Stopwatch.StartNew()
    terrain = document.GetElement(DB.ElementId(Int64(int(value))))
    if not isinstance(terrain, DB.Toposolid) or not terrain.UniqueId:
        raise ValueError("The ID must resolve to a Toposolid in the active project.")

    editor = terrain.GetSlabShapeEditor()
    if editor is None or not editor.IsEnabled:
        raise ValueError("The Toposolid has no enabled slab shape editor.")
    vertices = []
    for vertex in editor.SlabShapeVertices:
        if len(vertices) >= MAX_VERTICES:
            raise ValueError("The Toposolid has more than {} shape vertices.".format(MAX_VERTICES))
        if clock.ElapsedMilliseconds >= MAX_READ_MS:
            raise ValueError("Toposolid read exceeded {} ms after {} vertices.".format(MAX_READ_MS, len(vertices)))
        point = vertex.Position
        coordinates = [float(point.X), float(point.Y), float(point.Z)]
        if any(math.isnan(value) or math.isinf(value) for value in coordinates):
            raise ValueError("A Toposolid shape vertex has non-finite coordinates.")
        vertices.append(coordinates)
    if not vertices:
        raise ValueError("The enabled Toposolid shape editor returned no vertices.")

    payload = json.dumps({
        "document": {"uid": str(document.ProjectInformation.UniqueId),
                     "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()},
        "toposolid": {"id": str(terrain.Id.Value), "uid": str(terrain.UniqueId)},
        "vertices_ft": sorted(vertices),
    }, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("Toposolid inventory is {} bytes, above the {} byte limit.".format(
            len(payload), MAX_PAYLOAD_BYTES))
    if clock.ElapsedMilliseconds >= MAX_READ_MS:
        raise ValueError("Toposolid read exceeded {} ms after {} vertices.".format(MAX_READ_MS, len(vertices)))
    return {"status": "ok", "snapshot_json": payload}


if "inputs" in globals():
    try:
        result = read_vertices(doc, inputs)
    except ValueError as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not read the Toposolid shape vertices.",
                  "error_type": type(error).__name__}
