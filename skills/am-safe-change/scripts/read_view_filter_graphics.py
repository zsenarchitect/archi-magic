# -*- coding: utf-8 -*-
"""Read the native ordered filter graphics on explicitly identified Revit views."""

import json
import math

from pyrevit.api import DB

from duplicate_views import bounded, can_edit, document_identity, identifier, key, owner_state, viewports
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


def transfer_scope(document, request, clock):
    if (not isinstance(request, dict) or not {"source_id", "target_ids"}.issubset(request)
            or set(request) - {"source_id", "target_ids", "allow_placed"}
            or type(request.get("allow_placed", False)) is not bool):
        raise ValueError("Supply a source, explicit targets and optional placement approval.")
    target_ids = request["target_ids"]
    if not isinstance(target_ids, list) or not 1 <= len(target_ids) <= 20:
        raise ValueError("Supply one to 20 explicit target views or templates.")
    source_id = key(identifier(request["source_id"]))
    target_ids = [key(identifier(item)) for item in target_ids]
    if len(set([source_id] + target_ids)) != len(target_ids) + 1:
        raise ValueError("Source and target view IDs must be distinct.")
    views = {key(view.Id): view for view in bounded(DB.FilteredElementCollector(document).OfClass(DB.View),
                                                    10000, "project view", clock)}
    if source_id not in views or any(item not in views for item in target_ids):
        raise ValueError("Every requested view or template must exist in the saved project.")

    filter_group = str(int(DB.BuiltInParameter.VIS_GRAPHICS_FILTERS))
    controlled, consumers = {}, {}
    for view_id, view in views.items():
        if view.IsTemplate:
            continue
        template_id = key(view.ViewTemplateId)
        if template_id != "-1":
            consumers.setdefault(template_id, []).append(view_id)

    def controls_filters(template_id):
        if template_id not in controlled:
            template = views.get(template_id)
            if template is None or not template.IsTemplate:
                raise ValueError("A selected view has an unresolved template assignment.")
            groups = {key(item) for item in template.GetTemplateParameterIds()}
            exclusions = {key(item) for item in template.GetNonControlledTemplateParameterIds()}
            if not exclusions.issubset(groups):
                raise ValueError("A selected template has inconsistent controlled parameters.")
            controlled[template_id] = filter_group in groups - exclusions
        return controlled[template_id]

    def family(view_id):
        view = views[view_id]
        primary_id = key(view.GetPrimaryViewId())
        if primary_id == "-1":
            primary_id = view_id
        primary = views.get(primary_id)
        if primary is None or primary.IsTemplate or key(primary.GetPrimaryViewId()) != "-1":
            raise ValueError("A selected dependent view has no verified primary.")
        members = [primary_id] + [key(item) for item in bounded(primary.GetDependentViewIds(), 99,
                                                                  "dependent family", clock)]
        if view_id not in members or len(set(members)) != len(members):
            raise ValueError("A dependent view is missing from its primary's family.")
        for member_id in members[1:]:
            member = views.get(member_id)
            if (member is None or member.IsTemplate or key(member.GetPrimaryViewId()) != primary_id
                    or key(member.ViewTemplateId) not in ("-1", key(primary.ViewTemplateId))):
                raise ValueError("A dependent family's authority could not be verified.")
        return primary_id, members

    def authority(view_id):
        view = views[view_id]
        if view.IsTemplate:
            return view_id
        primary_id, _ = family(view_id)
        template_id = key(views[primary_id].ViewTemplateId)
        return template_id if template_id != "-1" and controls_filters(template_id) else primary_id

    source_authority = authority(source_id)
    affected = set()
    targets = []
    for target_id in target_ids:
        recipient = authority(target_id)
        if views[recipient].IsTemplate and target_id != recipient:
            raise ValueError("Target %s is filter-controlled by template %s. Select that template explicitly."
                             % (target_id, recipient))
        if views[recipient].IsTemplate:
            members = {recipient}
            if controls_filters(recipient):
                for consumer in bounded(consumers.get(recipient, []), 99, "template consumer", clock):
                    members.update(family(consumer)[1])
                    if len(members) > 100:
                        raise ValueError("The transfer affects more than 100 views.")
        else:
            members = set(family(recipient)[1])
        if source_id in members or source_authority in members or affected.intersection(members):
            raise ValueError("The source or multiple target scopes overlap.")
        affected.update(members)
        if len(affected) > 100:
            raise ValueError("The transfer affects more than 100 views.")
        targets.append({"requested_id": target_id, "recipient_id": recipient,
                        "affected_ids": sorted(members, key=int)})

    inspected = affected | {source_id, source_authority}
    if len(inspected) > 100:
        raise ValueError("The complete view inventory exceeds 100 views.")
    placements = viewports(document, [item for item in inspected if not views[item].IsTemplate], clock)
    metadata = {}
    for view_id in sorted(inspected, key=int):
        view = views[view_id]
        placed = placements.get(view_id, [])
        if not view.IsTemplate and bool(placed) != (str(view.GetPlacementOnSheetStatus()) != "NotPlaced"):
            raise ValueError("A selected view has an unverified sheet placement.")
        ownership = owner_state(document, view) if view_id in affected else None
        metadata[view_id] = {"authority_id": authority(view_id), "placements": placed,
                             "ownership": ownership, "writable": view_id in affected and can_edit(ownership)}
    return views, metadata, {"source_id": source_id, "source_authority_id": source_authority,
                             "allow_placed": request.get("allow_placed", False), "targets": targets}


def transfer_inventory(document, request, definitions, patterns, remaining, clock):
    native, metadata, scope = transfer_scope(document, request, clock)
    views = {}
    for view_id, detail in metadata.items():
        row = read_view(document, native[view_id], definitions, patterns, remaining, clock)
        row.update(detail)
        views[view_id] = row
    for row in views.values():
        controller = views.get(row["authority_id"])
        if controller is None or controller["filters"] != row["filters"]:
            raise ValueError("A view's filter graphics do not match its identified authority.")
    return {"kind": "view-filter-transfer-inventory", "scope": scope, "views": views}


def main(document, args):
    project = document_identity(document)
    clock = Stopwatch.StartNew()
    definitions, patterns, remaining = {}, {}, [4096]
    if "request" in args:
        data = transfer_inventory(document, args["request"], definitions, patterns, remaining, clock)
    else:
        requested = args["view_ids"]
        if not isinstance(requested, list) or not 1 <= len(requested) <= 21:
            raise ValueError("Supply one to 21 explicit view or template IDs.")
        native_ids = [identifier(item) for item in requested]
        if len({key(item) for item in native_ids}) != len(native_ids):
            raise ValueError("View IDs must be distinct.")
        data = {"kind": "view-filter-graphics-inventory",
                "views": [read_view(document, document.GetElement(item), definitions, patterns, remaining, clock)
                          for item in native_ids]}
    data.update(document=project, filter_definitions=definitions, pattern_definitions=patterns)
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(payload) > MAX_BYTES or clock.ElapsedMilliseconds > 10000:
        raise ValueError("The filter inventory exceeds its size or time budget.")
    return {"status": "ok", "inventory_json": payload}


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception:
        result = {"status": "stopped", "message": "Revit could not read the selected view filter graphics."}
