# -*- coding: utf-8 -*-
"""Read exact instance and type values for the proposed view coloring."""

import json
import re

from System import Double, Int32, Int64
from System.Collections.Generic import HashSet, List
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

from duplicate_views import bounded, document_identity, identifier, key
from read_view_filter_graphics import filter_definition, graphics, native_number, rule_signature
from view_duplicate_preview import MAX_BYTES, TEXT, snapshot_digest

SIGNED_ID = re.compile(r"^-?[1-9][0-9]{0,18}\Z")
STORAGE_TYPES = ("String", "Integer", "Double", "ElementId")


def signed_identifier(value):
    if (not isinstance(value, TEXT) or not SIGNED_ID.match(value) or value == "-1"
            or not -2**63 <= int(value) < 2**63):
        raise ValueError("Use an exact signed Revit category or parameter ID.")
    return DB.ElementId(Int64(int(value)))


def selected_parameter(element, parameter_id, clock):
    matches = [parameter for parameter in bounded(element.Parameters, 512, "element parameter", clock)
               if key(parameter.Id) == parameter_id]
    if len(matches) > 1:
        raise ValueError("Revit reported ambiguous parameters with ID %s." % parameter_id)
    return matches[0] if matches else None


def parameter_state(document, parameter):
    if parameter is None:
        return None
    storage = str(parameter.StorageType)
    row = {"id": key(parameter.Id), "guid": str(parameter.GUID) if parameter.IsShared else None,
           "storage": storage, "spec": str(parameter.Definition.GetDataType().TypeId),
           "has_value": bool(parameter.HasValue), "value": None, "label": None}
    if storage not in STORAGE_TYPES:
        raise ValueError("The selected parameter has unsupported storage.")
    if not row["has_value"]:
        return row
    if storage == "String":
        value = parameter.AsString()
        if value is None:
            raise ValueError("Revit reported a populated text parameter without a string value.")
        row["value"] = str(value)
        row["label"] = row["value"]
    elif storage == "Integer":
        row["value"] = int(parameter.AsInteger())
        row["label"] = parameter.AsValueString() or str(row["value"])
    elif storage == "Double":
        row["value"] = native_number(parameter.AsDouble())
        row["label"] = parameter.AsValueString() or str(row["value"])
    else:
        value = parameter.AsElementId()
        row["value"] = key(value)
        row["label"] = row["value"]
        if value.Value > 0:
            reference = document.GetElement(value)
            if reference is None:
                raise ValueError("A populated element reference could not be resolved.")
            row["reference_uid"] = str(reference.UniqueId)
    if len(row["label"]) > 1024:
        raise ValueError("A parameter label exceeds the inventory limit.")
    return row


def element_row(document, element, parameter_id, types, clock):
    type_id = element.GetTypeId()
    type_key = key(type_id)
    if type_id.Value > 0 and type_key not in types:
        element_type = document.GetElement(type_id)
        if element_type is None:
            raise ValueError("An observed element has an unresolved type.")
        types[type_key] = {"uid": str(element_type.UniqueId), "parameter": parameter_state(
            document, selected_parameter(element_type, parameter_id, clock))}
    instance = parameter_state(document, selected_parameter(element, parameter_id, clock))
    type_row = types.get(type_key)
    type_value = type_row["parameter"] if type_row is not None else None
    if instance and type_value and any(instance[field] != type_value[field] for field in ("guid", "storage", "spec")):
        raise ValueError("Instance and type expose incompatible definitions for parameter %s." % parameter_id)
    if instance is not None and instance["has_value"]:
        source = "instance"
    elif type_value is not None and type_value["has_value"]:
        source = "type"
    elif instance is not None or type_value is not None:
        source = "unset"
    else:
        source = "missing"
    return {"id": key(element.Id), "uid": str(element.UniqueId), "category_id": key(element.Category.Id),
            "type_id": type_key, "instance": instance, "source": source}


def value_groups(rows, types, epsilon):
    definitions, groups, expected = set(), {}, {}
    for row in rows:
        type_value = types.get(row["type_id"], {}).get("parameter")
        state = type_value if row["source"] == "type" else (row["instance"] or type_value)
        definitions.add((state["guid"], state["storage"], state["spec"]))
        value = state["value"]
        # IEEE signed zeros have one numeric equality value in Revit.
        if state["storage"] == "Double" and value == 0.0:
            value = 0.0
        typed = {"storage": state["storage"], "has_value": row["source"] != "unset", "value": value}
        group_id = snapshot_digest(typed)
        group = groups.setdefault(group_id, dict(typed, key=group_id, count=0, labels=set(), member_ids=[]))
        if typed["storage"] == "Double" and typed["has_value"]:
            group["epsilon"] = epsilon
        group["count"] += 1
        group["member_ids"].append(row["id"])
        if state["label"] is not None:
            group["labels"].add(state["label"])
        expected[row["id"]] = group_id
        if len(groups) > 24:
            raise ValueError("The view contains more than 24 distinct parameter values.")
    if len(definitions) != 1:
        raise ValueError("The selected elements do not share one compatible parameter definition.")
    result = [groups[group_id] for group_id in sorted(groups)]
    for group in result:
        group["labels"] = sorted(group["labels"])
    return result, expected


def rule_for_value(parameter, group, strategy, resources):
    factory = DB.ParameterFilterRuleFactory
    if strategy == "no_value":
        return factory.CreateHasNoValueParameterRule(parameter)
    if strategy == "inverse_has_value":
        inner = factory.CreateHasValueParameterRule(parameter)
        resources.append(inner)
        return DB.FilterInverseRule(inner)
    storage, value = group["storage"], group["value"]
    if storage == "String":
        return factory.CreateEqualsRule(parameter, value)
    if storage == "Integer":
        return factory.CreateEqualsRule(parameter, Int32(value))
    if storage == "Double":
        return factory.CreateEqualsRule(parameter, Double(value), Double(group["epsilon"]))
    return factory.CreateEqualsRule(parameter, DB.ElementId(Int64(int(value))))


def attachable_expression(document, parameter, categories, category_set, group, retained, clock):
    strategies = ("equals",) if group["has_value"] else ("no_value", "inverse_has_value")
    api = DB.ParameterFilterElement
    for strategy in strategies:
        resources = []
        accepted = False
        try:
            rule = rule_for_value(parameter, group, strategy, resources)
            resources.append(rule)
            expression = DB.ElementParameterFilter(rule)
            resources.append(expression)
            if (api.ElementFilterIsAcceptableForParameterFilterElement(document, category_set, expression)
                    and api.AllRuleParametersApplicable(document, categories, expression)):
                group["strategy"] = strategy
                group["expression"] = {"type": "ElementParameterFilter", "inverted": bool(expression.Inverted),
                                       "rules": [rule_signature(item, 0, [64], clock)
                                                 for item in bounded(expression.GetRules(), 16, "value rule", clock)]}
                retained.extend(resources)
                accepted = True
                return expression
        finally:
            if not accepted:
                for item in reversed(resources):
                    item.Dispose()
    return None


def qualify_rules(document, parameter_id, category_ids, rows, types, missing, epsilon, clock):
    if not rows or missing:
        return {"status": "blocked", "reason": "missing_parameter" if missing else "empty_scope",
                "groups": [], "mismatch_count": 0, "mismatches": []}
    groups, expected = value_groups(rows, types, epsilon)
    category_set = HashSet[DB.ElementId]()
    for item in category_ids:
        category_set.Add(item)
    parameter = signed_identifier(parameter_id)
    expressions, unattached, retained = {}, [], []
    mismatches, mismatch_count = [], 0
    try:
        for group in groups:
            expression = attachable_expression(document, parameter, category_ids, category_set, group, retained, clock)
            if expression is None:
                unattached.append(group["key"])
            else:
                expressions[group["key"]] = expression
        if unattached:
            return {"status": "blocked", "reason": "unattachable_rule", "unattachable_keys": unattached,
                    "groups": groups, "mismatch_count": 0, "mismatches": []}

        predicates = [(group["key"], expressions[group["key"]]) for group in groups]
        for row in bounded(rows, 5000, "native rule candidate", clock):
            element = document.GetElement(identifier(row["id"]))
            if element is None or str(element.UniqueId) != row["uid"]:
                raise ValueError("A view element changed while verifying the proposed rules.")
            matched = [group_id for group_id, expression in predicates if expression.PassesFilter(element)]
            intended = expected[row["id"]]
            if matched != [intended]:
                mismatch_count += 1
                if len(mismatches) < 50:
                    mismatches.append({"id": row["id"], "expected": intended, "matches": matched})
    finally:
        for item in reversed(retained):
            item.Dispose()
    return {"status": "blocked" if mismatch_count else "verified_host_partition",
            "reason": "native_rule_overlap_or_gap" if mismatch_count else None, "groups": groups,
            "mismatch_count": mismatch_count, "mismatches": mismatches}


def existing_filter_matches(document, view, inventory, clock):
    from view_color_preview import normalized_rule

    groups = inventory["native_rules"]["groups"]
    categories = set(inventory["category_ids"])
    signatures = {snapshot_digest(normalized_rule(group["expression"])): group["key"] for group in groups}
    groups_by_members = {frozenset(group["member_ids"]): group["key"] for group in groups}
    ordered = [key(item) for item in bounded(view.GetOrderedFilters(), 100, "applied filter", clock)]
    applied = set(ordered)
    solid = key(DB.LinePatternElement.GetSolidPatternId())
    rows = inventory["rows"]
    observed = []
    for row in rows:
        element = document.GetElement(identifier(row["id"]))
        if element is None or str(element.UniqueId) != row["uid"]:
            raise ValueError("An observed element changed during filter reuse verification.")
        observed.append((row["id"], element))

    exact = {group["key"]: [] for group in groups}
    possible = {group["key"]: [] for group in groups}
    definition_budget, evaluation_budget = [4096], 120000 - len(groups) * len(rows)
    names = []
    collector = DB.FilteredElementCollector(document)
    try:
        for item in bounded(collector.OfClass(DB.ParameterFilterElement), 5000, "existing filter", clock):
            item_id = key(item.Id)
            name = str(item.Name)
            names.append({"id": item_id, "name": name})
            if {key(category) for category in bounded(item.GetCategories(), 256, "existing filter category", clock)} != categories:
                continue
            try:
                definition = filter_definition(item, definition_budget, clock)
                signature = snapshot_digest(normalized_rule(definition["tree"]))
            except ValueError:
                # A definition we cannot serialize may still be comparable on the observed elements.
                signature = None
            match = signatures.get(signature)
            if match is not None:
                entry = {"id": item_id, "uid": str(item.UniqueId), "name": name, "applied": item_id in applied}
                if entry["applied"]:
                    settings = view.GetFilterOverrides(item.Id)
                    try:
                        entry.update(visible=bool(view.GetFilterVisibility(item.Id)),
                                     enabled=bool(view.GetIsFilterEnabled(item.Id)),
                                     graphics=graphics(settings, solid))
                    finally:
                        settings.Dispose()
                exact[match].append(entry)
                continue

            expression = item.GetElementFilter()
            if expression is None:
                continue
            try:
                evaluation_budget -= len(observed)
                if evaluation_budget < 0:
                    raise ValueError("The existing filter comparison exceeds 120,000 native evaluations.")
                matching = frozenset(item_id for item_id, element in bounded(
                    observed, 5000, "existing filter candidate", clock) if expression.PassesFilter(element))
                match = groups_by_members.get(matching)
                if match is not None:
                    possible[match].append(item_id)
            finally:
                expression.Dispose()
    finally:
        collector.Dispose()

    results = {}
    for group in groups:
        group_id = group["key"]
        exact_rows = sorted(exact[group_id], key=lambda row: int(row["id"]))
        choices = [row for row in exact_rows if row["applied"]] or exact_rows
        uncertain = sorted(possible[group_id], key=int)
        selected = choices[0] if len(choices) == 1 else None
        if len(choices) > 1 or (not choices and uncertain):
            status, reason = "blocked", "ambiguous_filter_match"
        elif selected and selected["applied"] and not (selected["visible"] and selected["enabled"]):
            status, reason = "blocked", "existing_filter_hidden_or_disabled"
        else:
            status, reason = ("reuse" if selected else "create"), None
        results[group_id] = {"status": status, "reason": reason, "exact": exact_rows,
                             "uncertain_filter_ids": uncertain,
                             "selected_id": selected["id"] if status == "reuse" else None}
    return {"groups": results, "ordered_applied_ids": ordered,
            "existing_parameter_filter_names": sorted(names, key=lambda row: int(row["id"]))}


def main(document, args):
    project = document_identity(document)
    check_rules = args.get("check_rules", False)
    check_reuse = args.get("check_reuse", False)
    if type(check_rules) is not bool or type(check_reuse) is not bool or (check_reuse and not check_rules):
        raise ValueError("Use boolean rule and reuse flags. Reuse checking requires native rule checking.")
    epsilon = args.get("double_epsilon", 1e-9)
    if type(epsilon) not in (int, float) or not 0 < epsilon <= 1e-4:
        raise ValueError("Use a positive numeric double tolerance up to 0.0001 in Revit internal units.")
    view_id = identifier(args["view_id"])
    view = document.GetElement(view_id)
    if (not isinstance(view, DB.View) or view.IsTemplate or not view.AreGraphicsOverridesAllowed()
            or view.IsTemporaryViewPropertiesModeEnabled()
            or not DB.FilteredElementCollector.IsViewValidForElementIteration(document, view_id)):
        raise ValueError("Select a non-temporary project view with supported filter graphics and element iteration.")
    parameter_id = key(signed_identifier(args["parameter_id"]))
    selected = args["category_ids"]
    if not isinstance(selected, list) or not 1 <= len(selected) <= 20 or len(set(selected)) != len(selected):
        raise ValueError("Select one to 20 distinct native categories.")
    category_ids = List[DB.ElementId]()
    for item in selected:
        category_ids.Add(signed_identifier(item))
    filterable = {key(item) for item in DB.ParameterFilterUtilities.GetFilterableParametersInCommon(document, category_ids)}
    if parameter_id not in filterable:
        raise ValueError("The parameter is not filterable across all selected categories.")
    clock = Stopwatch.StartNew()
    types, rows = {}, []
    category_filter = DB.ElementMulticategoryFilter(category_ids)
    collector = DB.FilteredElementCollector(document, view_id)
    try:
        candidates = collector.WhereElementIsNotElementType().WherePasses(category_filter)
        for element in bounded(candidates, 5000, "view candidate", clock):
            rows.append(element_row(document, element, parameter_id, types, clock))
    finally:
        collector.Dispose()
        category_filter.Dispose()
    links = DB.FilteredElementCollector(document, view_id)
    try:
        link_ids = [key(link.Id) for link in bounded(links.OfClass(DB.RevitLinkInstance), 100, "view link", clock)]
    finally:
        links.Dispose()
    missing = [row["id"] for row in rows if row["source"] == "missing"]
    data = {"kind": "view-color-parameter-inventory", "document": project,
            "view": {"id": key(view.Id), "uid": str(view.UniqueId)}, "parameter_id": parameter_id,
            "category_ids": sorted(selected, key=int), "scope": "potentially_visible_host_elements",
            "rows": sorted(rows, key=lambda row: int(row["id"])), "types": types, "missing_parameter_ids": sorted(missing, key=int),
            "link_candidate_ids": sorted(link_ids, key=int)}
    if check_rules:
        data["native_rules"] = qualify_rules(document, parameter_id, category_ids, data["rows"], types, missing,
                                             float(epsilon), clock)
    if check_reuse and data["native_rules"]["status"] == "verified_host_partition":
        data["existing_filters"] = existing_filter_matches(document, view, data, clock)
    if "legend_types" in args:
        from read_color_legend import legend_resources
        from view_color_preview import legend_name, workflow_identity

        data["legend_preflight"] = legend_resources(document, legend_name(workflow_identity(data)),
                                                     args["legend_types"], clock)
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(payload) > MAX_BYTES or clock.ElapsedMilliseconds > 10000:
        raise ValueError("The parameter inventory exceeds its size or time budget.")
    return {"status": "ok", "inventory_json": payload}


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception:
        result = {"status": "stopped", "message": "Revit could not verify the selected parameter inventory."}
