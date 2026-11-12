# -*- coding: utf-8 -*-
"""Read an active, staged Revit family's thumbnail settings and 3D extent."""

import hashlib
from itertools import product
import json
import math
import os
import re
import stat

from pyrevit.api import DB
from System.Diagnostics import Stopwatch

MAX_FILE = 128 * 1024 * 1024
MAX_ELEMENTS = 2000
MAX_OBJECTS = 5000
MAX_VIEWS = 100
TEXT = (str, type(u""))


def id_value(identifier):
    try:
        return str(identifier.Value)
    except AttributeError:
        return str(identifier.IntegerValue)


def triple(xyz):
    values = [float(xyz.X), float(xyz.Y), float(xyz.Z)]
    if any(math.isnan(value) or math.isinf(value) for value in values):
        raise ValueError("A Revit coordinate is not finite.")
    return values


def orientation(value):
    if value is None:
        return None
    return {"eye_ft": triple(value.EyePosition), "up": triple(value.UpDirection),
            "forward": triple(value.ForwardDirection)}


def staged_identity(document, request):
    if (document is None or not document.IsFamilyDocument or document.IsLinked
            or not document.PathName or document.OwnerFamily is None):
        raise ValueError("Open the reviewed staged Revit family.")
    path = os.path.normpath(document.PathName)
    if (not path.lower().endswith(".rfa")
            or not os.path.basename(os.path.dirname(path)).startswith("family-previews-")):
        raise ValueError("Open the staged family copy from the family preview workspace.")
    for key in ("staged_path_sha256", "staged_file_sha256"):
        if not isinstance(request.get(key), TEXT) or not re.match(r"^[0-9a-f]{64}\Z", request[key]):
            raise ValueError("Use the Revit inputs from the verified staging manifest.")
    path_sha = hashlib.sha256(os.path.normcase(path).encode("utf-8")).hexdigest()
    if path_sha != request["staged_path_sha256"]:
        raise ValueError("The active family path differs from the verified staged path.")
    info = os.lstat(path)
    if (not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not 0 < info.st_size <= MAX_FILE):
        raise ValueError("The staged family is unavailable or exceeds the size limit.")
    value = hashlib.sha256()
    total = 0
    with open(path, "rb") as stream:
        before = os.fstat(stream.fileno())
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE:
                raise ValueError("The staged family exceeds the size limit.")
            value.update(chunk)
        after = os.fstat(stream.fileno())
    final = os.lstat(path)
    signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime)
    if (total != info.st_size
            or any((row.st_dev, row.st_ino, row.st_size, row.st_mtime) != signature
                   for row in (before, after, final))
            or value.hexdigest() != request["staged_file_sha256"]):
        raise ValueError("The staged family bytes differ from the verified copy.")
    return {"staged_path_sha256": path_sha, "staged_file_sha256": value.hexdigest(),
            "family_uid": str(document.OwnerFamily.UniqueId)}


def background(view):
    style = view.GetBackground()
    if style is None:
        return None
    try:
        kind = str(style.Type)
        colors = {}
        if kind == "Gradient":
            for name in ("SkyColor", "BackgroundColor", "GroundColor"):
                color = getattr(style, name)
                colors[name] = [int(color.Red), int(color.Green), int(color.Blue)]
        return {"type": kind, "colors": colors}
    finally:
        style.Dispose()


def view_state(view):
    if view is None:
        return None
    state = {"id": id_value(view.Id), "uid": str(view.UniqueId), "name": view.Name,
             "type": str(view.ViewType), "template": bool(view.IsTemplate)}
    if isinstance(view, DB.View3D) and not view.IsTemplate:
        state.update({"perspective": bool(view.IsPerspective), "locked": bool(view.IsLocked),
                      "section_box": bool(view.IsSectionBoxActive), "crop_box": bool(view.CropBoxActive),
                      "view_template_id": id_value(view.ViewTemplateId),
                      "display_style": str(view.DisplayStyle), "background": background(view),
                      "orientation": orientation(view.GetOrientation()),
                      "saved_orientation_api": hasattr(view, "GetSavedOrientation"),
                      "saved_orientation": orientation(view.GetSavedOrientation())
                      if hasattr(view, "GetSavedOrientation") else None})
    return state


def available_view_types(document, clock):
    collector = DB.FilteredElementCollector(document)
    try:
        rows = []
        for index, item in enumerate(collector.OfClass(DB.ViewFamilyType)):
            if index >= MAX_VIEWS or clock.ElapsedMilliseconds >= 10000:
                raise ValueError("The family view type read exceeded its limits.")
            if item.ViewFamily == DB.ViewFamily.ThreeDimensional:
                rows.append({"id": id_value(item.Id), "uid": str(item.UniqueId)})
        return sorted(rows, key=lambda row: int(row["id"]))
    finally:
        collector.Dispose()


def solid_points(geometry, transform, budget, depth=0):
    if depth > 12:
        raise ValueError("Family geometry nesting exceeded its limit.")
    for obj in geometry:
        budget["objects"] += 1
        if budget["objects"] > MAX_OBJECTS or budget["clock"].ElapsedMilliseconds >= 10000:
            raise ValueError("The family geometry read exceeded its limits.")
        if isinstance(obj, DB.GeometryInstance):
            for point in solid_points(obj.GetSymbolGeometry(), transform.Multiply(obj.Transform), budget, depth + 1):
                yield point
        elif isinstance(obj, DB.Solid) and obj.Faces.Size and abs(obj.Volume) > 1e-12:
            bounds = obj.GetBoundingBox()
            try:
                to_model = transform.Multiply(bounds.Transform)
                axes = zip(triple(bounds.Min), triple(bounds.Max))
                for coords in product(*axes):
                    yield triple(to_model.OfPoint(DB.XYZ(*coords)))
            finally:
                bounds.Dispose()


def geometry_extent(document, view, clock):
    collector = DB.FilteredElementCollector(document, view.Id)
    options = DB.Options()
    budget = {"objects": 0, "clock": clock}
    low, high = None, None
    found = []
    try:
        options.View = view
        options.IncludeNonVisibleObjects = False
        for index, element in enumerate(collector.WhereElementIsNotElementType()):
            if index >= MAX_ELEMENTS or clock.ElapsedMilliseconds >= 10000:
                raise ValueError("The family geometry read exceeded its limits.")
            geometry = element.get_Geometry(options)
            if geometry is None:
                continue
            count = 0
            for point in solid_points(geometry, DB.Transform.Identity, budget):
                low = point if low is None else list(map(min, low, point))
                high = point if high is None else list(map(max, high, point))
                count += 1
            if count:
                found.append(id_value(element.Id))
    finally:
        options.Dispose()
        collector.Dispose()
    if clock.ElapsedMilliseconds >= 10000:
        raise ValueError("The family geometry read exceeded its limits.")
    return {"element_ids": sorted(found, key=int), "min_ft": low, "max_ft": high}


def read_family(document, request):
    identity = staged_identity(document, request)
    clock = Stopwatch.StartNew()
    view = document.ActiveView
    owner = document.OwnerFamily
    category = owner.FamilyCategory
    current_type = document.FamilyManager.CurrentType
    settings = document.GetDocumentPreviewSettings()
    try:
        selected = settings.PreviewViewId
        preview_view = None if id_value(selected) == "-1" else document.GetElement(selected)
        if id_value(selected) != "-1" and preview_view is None:
            raise ValueError("Revit did not resolve the family preview view.")
        forced = bool(settings.IsViewUpdateForced)
    finally:
        settings.Dispose()
    active_state = view_state(view)
    same_view = preview_view is not None and view is not None and id_value(selected) == id_value(view.Id)
    preview_state = active_state if same_view else view_state(preview_view)
    result = {"kind": "family-preview-snapshot", "identity": identity, "document_modified": bool(document.IsModified),
              "read_only": bool(document.IsReadOnly), "category_type": str(category.CategoryType) if category else None,
              "family_type": current_type.Name if current_type is not None else None,
              "active_view": active_state, "preview_view": preview_state, "force_preview_update": forced,
              "three_dimensional_view_types": available_view_types(document, clock),
              "geometry": None}
    if category and category.CategoryType == DB.CategoryType.Annotation:
        result["classification"] = "annotation"
    elif (not isinstance(view, DB.View3D) or view.IsTemplate or view.IsPerspective
            or view.IsSectionBoxActive or view.CropBoxActive
            or view.IsInTemporaryViewMode(DB.TemporaryViewMode.TemporaryHideIsolate)
            or view.IsInTemporaryViewMode(DB.TemporaryViewMode.RevealHiddenElements)
            or view.IsInTemporaryViewMode(DB.TemporaryViewMode.TemporaryViewProperties)
            or view.IsInTemporaryViewMode(DB.TemporaryViewMode.PreviewFamilyVisibility)
            or not DB.FilteredElementCollector.IsViewValidForElementIteration(document, view.Id)):
        result["classification"] = "uncropped_3d_view_required"
    else:
        result["geometry"] = geometry_extent(document, view, clock)
        result["classification"] = "solid_geometry_observed" if result["geometry"]["element_ids"] else "no_solid_in_view"
    return result


if "doc" in globals() and "inputs" in globals():
    try:
        snapshot = read_family(doc, inputs)
        result = {"status": "ok", "snapshot_json": json.dumps(snapshot, ensure_ascii=True, allow_nan=False)}
    except ValueError as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not read the staged family.",
                  "error_type": type(error).__name__}
