# -*- coding: utf-8 -*-
"""Create or reuse reviewed filters within the rollback-only color fixture."""

from System.Collections.Generic import HashSet, List
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

from duplicate_views import bounded, identifier, key
from read_color_parameter_values import attachable_expression, signed_identifier
from read_view_filter_graphics import filter_definition, graphics
from view_color_preview import normalized_rule

GRAPHIC_SETTERS = {
    "ProjectionLineColor": "SetProjectionLineColor",
    "CutLineColor": "SetCutLineColor",
    "SurfaceForegroundPatternColor": "SetSurfaceForegroundPatternColor",
    "CutForegroundPatternColor": "SetCutForegroundPatternColor",
}


def native_graphics(settings, solid):
    try:
        return graphics(settings, solid)
    finally:
        settings.Dispose()


def require_normal_display(view):
    manager = view.TemporaryViewModes
    for name in ("RevealHiddenElements", "TemporaryHideIsolate", "TemporaryViewProperties"):
        mode = getattr(DB.TemporaryViewMode, name)
        if manager is not None and manager.IsModeAvailable(mode) and manager.IsModeEnabled(mode):
            raise ValueError("Turn off temporary display modes before the color trial.")


def fixture_view(document, baseline, proposal):
    view = document.GetElement(identifier(baseline["view"]["id"]))
    if (not isinstance(view, DB.View) or str(view.UniqueId) != baseline["view"]["uid"] or view.IsTemplate
            or key(view.ViewTemplateId) != "-1" or key(view.GetPrimaryViewId()) != "-1"
            or list(view.GetDependentViewIds()) or str(view.GetPlacementOnSheetStatus()) != "NotPlaced"):
        raise ValueError("Use an independent, unplaced, untemplated fixture view.")
    require_normal_display(view)

    ordered = [key(item) for item in bounded(view.GetOrderedFilters(), 100, "fixture filter", Stopwatch.StartNew())]
    reviewed = {row["filter_id"] for row in proposal["legend_rows"] if row["filter_id"] is not None}
    if not set(ordered).issubset(reviewed):
        raise ValueError("The fixture has other applied filters whose effective precedence needs review.")
    solid = key(DB.LinePatternElement.GetSolidPatternId())
    defaults = native_graphics(DB.OverrideGraphicSettings(), solid)
    for row in bounded(baseline["rows"], 5000, "fixture element", Stopwatch.StartNew()):
        element = document.GetElement(identifier(row["id"]))
        if element is None or str(element.UniqueId) != row["uid"] or element.IsHidden(view):
            raise ValueError("A reviewed fixture element is missing, changed, or hidden.")
        if native_graphics(view.GetElementOverrides(element.Id), solid) != defaults:
            raise ValueError("A reviewed fixture element has individual graphic overrides.")
    return view, ordered


def create_filter(document, baseline, name, group, clock):
    categories, category_set = List[DB.ElementId](), HashSet[DB.ElementId]()
    for category_id in baseline["category_ids"]:
        native = signed_identifier(category_id)
        categories.Add(native)
        category_set.Add(native)
    retained = []
    try:
        actual_group = dict(group)
        expression = attachable_expression(document, signed_identifier(baseline["parameter_id"]),
                                           categories, category_set, actual_group, retained, clock)
        if expression is None or actual_group != group:
            raise ValueError("The reviewed rule could not be recreated exactly.")
        if not DB.NamingUtils.IsValidName(name):
            raise ValueError("The proposed filter name is invalid in Revit.")
        return DB.ParameterFilterElement.Create(document, name, categories, expression)
    finally:
        for item in reversed(retained):
            item.Dispose()


def apply_filters(document, view, original_order, baseline, proposal):
    by_key = {group["key"]: group for group in baseline["native_rules"]["groups"]}
    solid_id = proposal["legend_preflight"]["resources"]["solid"]["id"]
    native_solid = identifier(solid_id)
    solid_line = key(DB.LinePatternElement.GetSolidPatternId())
    order = list(original_order)
    states, added, changed = [], set(), False
    clock = Stopwatch.StartNew()
    for row in bounded(proposal["legend_rows"], 24, "trial color filter", clock):
        group = by_key[row["value_key"]]
        existing = row["filter_id"] is not None
        item = (document.GetElement(identifier(row["filter_id"])) if existing
                else create_filter(document, baseline, row["filter_name"], group, clock))
        if not isinstance(item, DB.ParameterFilterElement):
            raise ValueError("Revit did not resolve the reviewed parameter filter.")
        if existing:
            candidates = baseline["existing_filters"]["groups"][group["key"]]["exact"]
            expected = next(candidate for candidate in candidates if candidate["id"] == row["filter_id"])
            if str(item.UniqueId) != expected["uid"]:
                raise ValueError("An existing filter identity changed.")
        item_id = key(item.Id)
        if not existing:
            added.add(item_id)
        if item_id not in order:
            view.AddFilter(item.Id)
            view.SetFilterVisibility(item.Id, True)
            view.SetIsFilterEnabled(item.Id, True)
            order.append(item_id)
            changed = True
        if not (view.GetFilterVisibility(item.Id) and view.GetIsFilterEnabled(item.Id)):
            raise ValueError("The reviewed filter is hidden or disabled.")
        settings = view.GetFilterOverrides(item.Id)
        try:
            original = graphics(settings, solid_line)
            expected_graphics = dict(original)
            expected_graphics.update({field: row["rgb"] for field in GRAPHIC_SETTERS})
            for side in ("Surface", "Cut"):
                expected_graphics[side + "ForegroundPatternId"] = {"kind": "element", "id": solid_id}
                expected_graphics["Is" + side + "ForegroundPatternVisible"] = True
            if original != expected_graphics:
                rgb = DB.Color(*row["rgb"])
                for setter in GRAPHIC_SETTERS.values():
                    getattr(settings, setter)(rgb)
                settings.SetSurfaceForegroundPatternId(native_solid)
                settings.SetCutForegroundPatternId(native_solid)
                settings.SetSurfaceForegroundPatternVisible(True)
                settings.SetCutForegroundPatternVisible(True)
                view.SetFilterOverrides(item.Id, settings)
                changed = True
        finally:
            settings.Dispose()
        states.append({"value_key": group["key"], "filter_id": item_id, "filter_uid": str(item.UniqueId),
                       "name": row["filter_name"], "expression": normalized_rule(group["expression"]),
                       "graphics": expected_graphics, "created": not existing})
    return {"states": states, "added": added, "order": order, "changed": changed}


def verify_filters(document, view, baseline, trial):
    clock = Stopwatch.StartNew()
    if [key(item) for item in bounded(view.GetOrderedFilters(), 100, "trial filter readback", clock)] != trial["order"]:
        raise ValueError("Revit did not preserve the reviewed filter order.")
    solid = key(DB.LinePatternElement.GetSolidPatternId())
    remaining = [4096]
    for state in trial["states"]:
        item = document.GetElement(identifier(state["filter_id"]))
        if item is None or str(item.UniqueId) != state["filter_uid"] or str(item.Name) != state["name"]:
            raise ValueError("A native filter differs from its reviewed identity.")
        definition = filter_definition(item, remaining, clock)
        if (definition["categories"] != baseline["category_ids"]
                or normalized_rule(definition["tree"]) != state["expression"]
                or native_graphics(view.GetFilterOverrides(item.Id), solid) != state["graphics"]
                or not (view.GetFilterVisibility(item.Id) and view.GetIsFilterEnabled(item.Id))):
            raise ValueError("A native filter differs from its reviewed rules or graphics.")
    return trial["states"]
