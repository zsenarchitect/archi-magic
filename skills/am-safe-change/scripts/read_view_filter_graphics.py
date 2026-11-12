# -*- coding: utf-8 -*-
"""Read the native ordered filter graphics on explicitly identified Revit views."""

import json
import math

from pyrevit.api import DB

from duplicate_views import bounded, document_identity, identifier, key
from System import Int64
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


def native_number(value):
    result = float(value)
    if math.isnan(result) or math.isinf(result):
        raise ValueError("A filter rule or pattern has a nonfinite value.")
    return result


def reserve(remaining, count):
    remaining[0] -= count
    if remaining[0] < 0:
        raise ValueError("The filter and pattern definitions exceed the inventory budget.")


def ids(items, limit, label, remaining, clock):
    result = sorted((key(item) for item in bounded(items, limit, label, clock)), key=int)
    reserve(remaining, len(result))
    return result


def count_node(depth, remaining):
    if depth > 10:
        raise ValueError("The filter rule tree exceeds the inventory depth.")
    reserve(remaining, 1)


def rule_text(value):
    value = str(value)
    if len(value) > 4096:
        raise ValueError("A filter rule string exceeds the inventory limit.")
    return value


def rule_signature(rule, depth, remaining, clock):
    try:
        count_node(depth, remaining)
        kind = str(rule.GetType().Name)
        row = {"type": kind, "parameter": key(rule.GetRuleParameter())}
        if kind == "FilterInverseRule":
            row["inner"] = rule_signature(rule.GetInnerRule(), depth + 1, remaining, clock)
        elif kind == "FilterCategoryRule":
            row["categories"] = ids(rule.GetCategories(), 256, "rule category", remaining, clock)
        elif kind == "SharedParameterApplicableRule":
            row["name"] = rule_text(rule.ParameterName)
        elif kind in ("FilterStringRule", "FilterDoubleRule", "FilterIntegerRule",
                      "FilterElementIdRule", "FilterGlobalParameterAssociationRule"):
            row["evaluator"] = str(rule.GetEvaluator().GetType().FullName)
            if kind == "FilterStringRule":
                row["value"] = rule_text(rule.RuleString)
            elif kind == "FilterDoubleRule":
                row["value"], row["epsilon"] = native_number(rule.RuleValue), native_number(rule.Epsilon)
            elif kind == "FilterIntegerRule":
                row["value"] = int(rule.RuleValue)
            else:
                row["value"] = key(rule.RuleValue)
        elif kind not in ("HasValueFilterRule", "HasNoValueFilterRule"):
            raise ValueError("Unsupported native filter rule: " + kind)
        return row
    finally:
        rule.Dispose()


def filter_tree(expression, depth, remaining, clock):
    if expression is None:
        return None
    try:
        count_node(depth, remaining)
        kind = str(expression.GetType().Name)
        row = {"type": kind, "inverted": bool(expression.Inverted)}
        if kind in ("LogicalAndFilter", "LogicalOrFilter"):
            row["children"] = [filter_tree(child, depth + 1, remaining, clock)
                               for child in bounded(expression.GetFilters(), 256, "filter child", clock)]
        elif kind == "ElementParameterFilter":
            row["rules"] = [rule_signature(rule, depth + 1, remaining, clock)
                            for rule in bounded(expression.GetRules(), 256, "filter rule", clock)]
        else:
            raise ValueError("Unsupported native filter expression: " + kind)
        return row
    finally:
        expression.Dispose()


def filter_definition(element, remaining, clock):
    if isinstance(element, DB.SelectionFilterElement):
        return {"kind": "selection", "members": ids(element.GetElementIds(), 1000, "filter member", remaining, clock)}
    if isinstance(element, DB.ParameterFilterElement):
        return {"kind": "parameter", "categories": ids(element.GetCategories(), 256, "filter category", remaining, clock),
                "tree": filter_tree(element.GetElementFilter(), 0, remaining, clock)}
    raise ValueError("Unsupported native filter element.")


def fill_grid(grid, remaining, clock):
    try:
        segments = [native_number(item) for item in bounded(grid.GetSegments(), 256, "fill pattern segment", clock)]
        reserve(remaining, len(segments) + 1)
        return {"angle": native_number(grid.Angle), "offset": native_number(grid.Offset),
                "shift": native_number(grid.Shift),
                "origin": [native_number(grid.Origin.U), native_number(grid.Origin.V)],
                "segments": segments}
    finally:
        grid.Dispose()


def pattern_definition(document, pattern_id, is_line, remaining, clock):
    native_id = DB.ElementId(Int64(int(pattern_id)))
    element = document.GetElement(native_id)
    if is_line:
        if (element is not None and not isinstance(element, DB.LinePatternElement)) or (
                element is None and int(pattern_id) > 0):
            raise ValueError("The line pattern definition could not be resolved.")
        pattern = DB.LinePatternElement.GetLinePattern(document, native_id)
        if pattern is None:
            raise ValueError("The line pattern definition could not be resolved.")
        try:
            segments = [[str(item.Type), native_number(item.Length)]
                        for item in bounded(pattern.GetSegments(), 256, "line pattern segment", clock)]
            reserve(remaining, len(segments) + 1)
            return {"kind": "line" if element is not None else "built_in_line",
                    "uid": str(element.UniqueId) if element is not None else None, "name": str(pattern.Name),
                    "segments": segments}
        finally:
            pattern.Dispose()
    if not isinstance(element, DB.FillPatternElement):
        raise ValueError("The fill pattern definition could not be resolved.")
    pattern = element.GetFillPattern()
    try:
        if str(pattern.Target) != "Drafting":
            raise ValueError("View filter overrides require drafting fill patterns.")
        return {"kind": "fill", "uid": str(element.UniqueId), "name": str(pattern.Name),
                "target": str(pattern.Target), "orientation": str(pattern.HostOrientation),
                "solid": bool(pattern.IsSolidFill),
                "grids": [fill_grid(grid, remaining, clock)
                          for grid in bounded(pattern.GetFillGrids(), 128, "fill pattern grid", clock)]}
    finally:
        pattern.Dispose()


def read_view(document, view, definitions, patterns, remaining, clock):
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
        if filter_id not in definitions:
            definitions[filter_id] = filter_definition(element, remaining, clock)
        settings = view.GetFilterOverrides(native_id)
        try:
            captured = graphics(settings, solid)
            for field in PATTERNS:
                reference = captured[field]
                if reference is None or reference["kind"] != "element":
                    continue
                pattern_id = reference["id"]
                is_line = field.endswith("LinePatternId")
                if pattern_id not in patterns:
                    patterns[pattern_id] = pattern_definition(document, pattern_id, is_line, remaining, clock)
                if (patterns[pattern_id]["kind"] != "fill") != is_line:
                    raise ValueError("A pattern is referenced as both a fill and a line pattern.")
            rows.append({"id": filter_id, "uid": str(element.UniqueId), "name": str(element.Name),
                         "visible": bool(view.GetFilterVisibility(native_id)),
                         "enabled": bool(view.GetIsFilterEnabled(native_id)),
                         "graphics": captured})
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
    definitions, patterns, remaining = {}, {}, [4096]
    views = [read_view(document, document.GetElement(item), definitions, patterns, remaining, clock) for item in ids]
    payload = json.dumps({"kind": "view-filter-graphics-inventory", "document": project, "views": views,
                          "filter_definitions": definitions, "pattern_definitions": patterns},
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
