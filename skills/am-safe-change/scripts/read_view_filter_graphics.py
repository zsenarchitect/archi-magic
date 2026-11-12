# -*- coding: utf-8 -*-
"""Read the native ordered filter graphics on explicitly identified Revit views."""

import json

from pyrevit.api import DB

from duplicate_views import bounded, document_identity, identifier, key
from System.Diagnostics import Stopwatch

MAX_BYTES = 2 * 1024 * 1024
COLORS = (
    "CutBackgroundPatternColor", "CutForegroundPatternColor", "CutLineColor", "ProjectionLineColor",
    "SurfaceBackgroundPatternColor", "SurfaceForegroundPatternColor",
)
PATTERNS = (
    "CutBackgroundPatternId", "CutForegroundPatternId", "CutLinePatternId", "ProjectionLinePatternId",
    "SurfaceBackgroundPatternId", "SurfaceForegroundPatternId",
)
SWITCHES = (
    "Halftone", "IsCutBackgroundPatternVisible", "IsCutForegroundPatternVisible",
    "IsSurfaceBackgroundPatternVisible", "IsSurfaceForegroundPatternVisible",
)


def color(value):
    return [int(value.Red), int(value.Green), int(value.Blue)] if value.IsValid else None


def graphics(settings, solid):
    if not settings.IsValidObject:
        raise ValueError("Revit returned an invalid graphic override object.")
    result = {name: color(getattr(settings, name)) for name in COLORS}
    for name in PATTERNS:
        pattern = key(getattr(settings, name))
        result[name] = (None if pattern == "-1" else
                        {"kind": "solid_line"} if name.endswith("LinePatternId") and pattern == solid
                        else {"kind": "element", "id": pattern})
    result.update({name: bool(getattr(settings, name)) for name in SWITCHES})
    result.update({name: int(getattr(settings, name)) for name in
                   ("CutLineWeight", "ProjectionLineWeight", "Transparency")})
    result["DetailLevel"] = str(settings.DetailLevel)
    return result


def read_view(document, view, clock):
    if not isinstance(view, DB.View) or not view.AreGraphicsOverridesAllowed():
        raise ValueError("Each selected view must support Visibility/Graphics.")
    if not view.IsTemplate and view.IsTemporaryViewPropertiesModeEnabled():
        raise ValueError("Turn off Temporary View Properties before reading the selected view.")
    solid = key(DB.LinePatternElement.GetSolidPatternId())
    rows = []
    seen = set()
    for native_id in bounded(view.GetOrderedFilters(), 100, "view filter", clock):
        element = document.GetElement(native_id)
        filter_id = key(native_id)
        if not isinstance(element, DB.FilterElement) or filter_id in seen:
            raise ValueError("The view filter stack has an invalid or repeated element.")
        seen.add(filter_id)
        settings = view.GetFilterOverrides(native_id)
        try:
            rows.append({"id": filter_id, "uid": str(element.UniqueId), "name": str(element.Name),
                         "visible": bool(view.GetFilterVisibility(native_id)),
                         "enabled": bool(view.GetIsFilterEnabled(native_id)),
                         "graphics": graphics(settings, solid)})
        finally:
            settings.Dispose()
    return {"id": key(view.Id), "uid": str(view.UniqueId), "name": str(view.Name),
            "is_template": bool(view.IsTemplate), "template_id": key(view.ViewTemplateId), "filters": rows}


def main(document, args):
    project = document_identity(document)
    requested = args["view_ids"]
    if not isinstance(requested, list) or not 1 <= len(requested) <= 21:
        raise ValueError("Supply one to 21 explicit view or template IDs.")
    ids = [identifier(item) for item in requested]
    if len({key(item) for item in ids}) != len(ids):
        raise ValueError("View IDs must be distinct.")
    clock = Stopwatch.StartNew()
    views = [read_view(document, document.GetElement(item), clock) for item in ids]
    payload = json.dumps({"kind": "view-filter-graphics-inventory", "document": project, "views": views},
                         ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(payload) > MAX_BYTES:
        raise ValueError("The graphic override inventory exceeds 2 MiB.")
    return {"status": "ok", "inventory_json": payload}


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception:
        result = {"status": "stopped", "message": "Revit could not read the selected view filter graphics."}
