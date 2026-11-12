import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class ElementId:
    def __init__(self, value):
        self.Value = int(value)


class NativeList:
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self):
        self.values = []

    def Add(self, item):
        self.values.append(item.Value)

    def __iter__(self):
        return (ElementId(value) for value in self.values)


class View:
    Id = ElementId(31)
    UniqueId = "view-31"
    IsTemplate = False

    def __init__(self, applied=()):
        self.applied = dict(applied)

    def GetOrderedFilters(self):
        return [ElementId(value) for value in self.applied]

    def GetFilterOverrides(self, identifier):
        return NS(values={"sample": identifier.Value}, Dispose=lambda: None)

    def GetFilterVisibility(self, identifier):
        return self.applied[identifier.Value][0]

    def GetIsFilterEnabled(self, identifier):
        return self.applied[identifier.Value][1]

    def AreGraphicsOverridesAllowed(self):
        return True

    def IsTemporaryViewPropertiesModeEnabled(self):
        return False


class Link:
    Id = ElementId(99)


class Collector:
    def __init__(self, document, view_id=None):
        assert view_id is None or view_id.Value == 31
        self.document = document

    @staticmethod
    def IsViewValidForElementIteration(_document, view_id):
        return view_id.Value == 31

    def WhereElementIsNotElementType(self):
        return self

    def WherePasses(self, filters):
        assert filters.ids == [-200]
        return self.document.candidates

    def OfClass(self, kind):
        return [Link()] if kind is Link else self.document.existing_filters

    def Dispose(self):
        self.document.collectors_closed += 1


@pytest.fixture
def reader(monkeypatch):
    def bounded(items, limit, label, clock):
        for index, value in enumerate(items):
            if index >= limit or clock.ElapsedMilliseconds > 10000:
                raise ValueError(label)
            yield value

    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "System", NS(Double=float, Int32=int, Int64=int))
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(HashSet=NativeList, List=NativeList))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=1))))
    db = NS(ElementId=ElementId, View=View, RevitLinkInstance=Link, FilteredElementCollector=Collector,
            ElementMulticategoryFilter=lambda ids: NS(ids=ids.values, Dispose=lambda: None),
            ParameterFilterUtilities=NS(GetFilterableParametersInCommon=lambda _doc, _cats: [ElementId(-55)]),
            LinePatternElement=NS(GetSolidPatternId=lambda: ElementId(-3000001)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "duplicate_views", NS(
        bounded=bounded, document_identity=lambda _doc: {"project_uid": "project-1"},
        identifier=ElementId, key=lambda value: str(value.Value)))
    monkeypatch.setitem(sys.modules, "read_view_filter_graphics", NS(native_number=float, rule_signature=lambda rule, *_: dict(rule.signature),
                                                      graphics=lambda settings, _solid: settings.values,
                                                      filter_definition=lambda item, *_: {"tree": item.tree}))
    return runpy.run_path(str(SCRIPTS / "read_color_parameter_values.py"))


def parameter(storage, value, has_value=True, param_id=-55):
    return NS(Id=ElementId(param_id), IsShared=False, HasValue=has_value, StorageType=storage,
              Definition=NS(GetDataType=lambda: NS(TypeId="spec-" + storage)),
              AsString=lambda: value, AsInteger=lambda: value, AsDouble=lambda: value,
              AsElementId=lambda: ElementId(value), AsValueString=lambda: "label")


def element(identifier, parameters, type_id=-1):
    return NS(Id=ElementId(identifier), UniqueId="element-%d" % identifier, Parameters=parameters,
              Category=NS(Id=ElementId(-200)), GetTypeId=lambda: ElementId(type_id))


def test_native_inventory_separates_inherited_empty_unset_missing_and_special_values(reader):
    values = {
        31: View(),
        101: element(101, [parameter("String", "ignored", param_id=-22), parameter("String", None, False)], 501),
        102: element(102, [parameter("String", "")], 501),
        103: element(103, [parameter("String", None, False)]),
        104: element(104, []),
        105: element(105, [parameter("ElementId", -2000000)]),
        106: element(106, [], 501),
        501: element(501, [parameter("String", "type value")]),
    }
    document = NS(GetElement=lambda identifier: values.get(identifier.Value), collectors_closed=0,
                  candidates=[values[index] for index in range(101, 107)])
    result = reader["main"](document, {"view_id": "31", "parameter_id": "-55", "category_ids": ["-200"]})
    inventory = json.loads(result["inventory_json"])
    assert inventory["scope"] == "potentially_visible_host_elements"
    assert document.collectors_closed == 2
    assert [row["source"] for row in inventory["rows"]] == ["type", "instance", "unset", "missing", "instance", "type"]
    assert inventory["rows"][1]["instance"]["value"] == ""
    assert inventory["rows"][4]["instance"]["value"] == "-2000000"
    assert inventory["types"]["501"]["uid"] == "element-501"
    assert inventory["types"]["501"]["parameter"]["value"] == "type value"
    assert inventory["missing_parameter_ids"] == ["104"] and inventory["link_candidate_ids"] == ["99"]


def test_reader_rejects_ambiguous_parameter_id_and_unfilterable_selection(reader):
    row = element(101, [parameter("String", "one"), parameter("String", "two")])
    document = NS(GetElement=lambda _id: View(), collectors_closed=0, candidates=[row])
    with pytest.raises(ValueError, match="ambiguous"):
        reader["main"](document, {"view_id": "31", "parameter_id": "-55", "category_ids": ["-200"]})
    assert document.collectors_closed == 1
    with pytest.raises(ValueError, match="not filterable"):
        reader["main"](document, {"view_id": "31", "parameter_id": "-56", "category_ids": ["-200"]})
    assert document.collectors_closed == 1


def mock_rules(reader, *, direct_unset=False, inverse_unset=True, applicable=True):
    checked = []
    released = []

    class Rule:
        def __init__(self, strategy, value=None, epsilon=None):
            self.signature = {"strategy": strategy, "value": value, "epsilon": epsilon}

        def Dispose(self):
            pass

    class Expression:
        Inverted = False

        def __init__(self, rule):
            self.rule = rule

        def GetRules(self):
            return [self.rule]

        def PassesFilter(self, element):
            value = element.native_value
            rule = self.rule.signature
            if rule["strategy"] != "equals":
                return value is None
            intended = rule["value"]
            if isinstance(value, str) and isinstance(intended, str):
                return value.lower() == intended.lower()
            if rule["epsilon"] is not None:
                return value is not None and abs(value - intended) <= rule["epsilon"]
            return value == intended

        def Dispose(self):
            released.append(self.rule.signature["strategy"])

    def make_equals(parameter, value, epsilon=None):
        assert parameter.Value == -55
        return Rule("equals", value.Value if isinstance(value, ElementId) else value, epsilon)

    def eligible(_document, categories, expression):
        assert categories.values == [-200]
        strategy = expression.rule.signature["strategy"]
        checked.append(strategy)
        return {"equals": True, "no_value": direct_unset, "inverse_has_value": inverse_unset}[strategy]

    db = reader["DB"]
    db.ParameterFilterRuleFactory = NS(
        CreateEqualsRule=make_equals, CreateHasNoValueParameterRule=lambda _parameter: Rule("no_value"),
        CreateHasValueParameterRule=lambda _parameter: Rule("has_value"),
    )
    db.FilterInverseRule = lambda _inner: Rule("inverse_has_value")
    db.ElementParameterFilter = Expression
    db.ParameterFilterElement = NS(ElementFilterIsAcceptableForParameterFilterElement=eligible,
                                   AllRuleParametersApplicable=lambda *_: applicable)
    return checked, released


def qualifying_inventory(reader, candidates, types=(), epsilon=1e-9, existing=(), applied=(), reuse=False):
    values = {31: View(applied)}
    values.update({row.Id.Value: row for row in [*candidates, *types]})
    document = NS(GetElement=lambda identifier: values.get(identifier.Value), collectors_closed=0,
                  candidates=candidates, existing_filters=existing)
    result = reader["main"](document, {"view_id": "31", "parameter_id": "-55", "category_ids": ["-200"],
                                       "check_rules": True, "check_reuse": reuse, "double_epsilon": epsilon})
    return json.loads(result["inventory_json"])


def candidate(identifier, stored, native, type_id=-1):
    row = element(identifier, [stored] if stored is not None else [], type_id)
    row.native_value = native
    return row


def test_qualifies_attachable_exact_partition_and_unset_fallback(reader):
    checked, released = mock_rules(reader)
    inventory = qualifying_inventory(reader, [
        candidate(101, parameter("String", None, False), "type value", 501),
        candidate(102, parameter("String", ""), ""),
        candidate(103, parameter("String", None, False), None),
    ], [element(501, [parameter("String", "type value")])])
    result = inventory["native_rules"]
    assert result["status"] == "verified_host_partition" and result["mismatch_count"] == 0
    assert {group["value"]: (group["count"], group["strategy"]) for group in result["groups"]} == {
        None: (1, "inverse_has_value"), "": (1, "equals"), "type value": (1, "equals"),
    }
    assert sorted(checked) == sorted(released) == ["equals", "equals", "inverse_has_value", "no_value"]
    assert inventory["link_candidate_ids"] == ["99"]


@pytest.mark.parametrize("storage,values", [("String", ("North", "NORTH")), ("Double", (1.0, 1.0 + 5e-10))])
def test_native_case_and_double_overlap_block_partition(reader, storage, values):
    mock_rules(reader)
    rows = [candidate(index, parameter(storage, value), value) for index, value in enumerate(values, 101)]
    result = qualifying_inventory(reader, rows)["native_rules"]
    assert result["status"] == "blocked" and result["reason"] == "native_rule_overlap_or_gap"
    assert [item["id"] for item in result["mismatches"]] == ["101", "102"]
    assert all(len(item["matches"]) == 2 for item in result["mismatches"])


def test_native_applicability_and_missing_values_block_rule_qualification(reader):
    checked, released = mock_rules(reader, applicable=False)
    result = qualifying_inventory(reader, [candidate(101, parameter("ElementId", -2000000), -2000000)])["native_rules"]
    assert result["reason"] == "unattachable_rule" and len(result["unattachable_keys"]) == 1
    assert checked == released == ["equals"]
    result = qualifying_inventory(reader, [candidate(101, None, None)])["native_rules"]
    assert result["reason"] == "missing_parameter" and checked == ["equals"]


def existing_filter(identifier, value, *, category=-200, wrapped=False):
    rule = {"type": "ElementParameterFilter", "inverted": False,
            "rules": [{"strategy": "equals", "value": value, "epsilon": None}]}
    tree = {"type": "LogicalAndFilter", "inverted": False, "children": [rule]} if wrapped else rule

    def matches(element):
        return element.native_value.lower() == value.lower()

    return NS(Id=ElementId(identifier), UniqueId="filter-%d" % identifier, Name="Filter %d" % identifier,
              tree=tree, GetCategories=lambda: [ElementId(category)],
              GetElementFilter=lambda: NS(PassesFilter=matches, Dispose=lambda: None))


def reuse_by_value(inventory):
    matches = inventory["existing_filters"]["groups"]
    return {group["value"]: matches[group["key"]] for group in inventory["native_rules"]["groups"]}


def test_reuse_prefers_applied_exact_match_and_checks_all_categories(reader):
    mock_rules(reader)
    rows = [candidate(101, parameter("String", "North"), "North"),
            candidate(102, parameter("String", "South"), "South")]
    result = qualifying_inventory(reader, rows, existing=[
        existing_filter(201, "North", wrapped=True), existing_filter(202, "North"),
        existing_filter(203, "South", category=-201),
    ], applied=[(201, (True, True))], reuse=True)
    matches = reuse_by_value(result)
    assert matches["North"]["status"] == "reuse" and matches["North"]["selected_id"] == "201"
    assert matches["North"]["exact"][0]["graphics"] == {"sample": 201}
    assert [row["id"] for row in matches["North"]["exact"]] == ["201", "202"]
    assert matches["South"]["status"] == "create"


@pytest.mark.parametrize("scenario,expected", [
    ("duplicate", "ambiguous_filter_match"), ("hidden", "existing_filter_hidden_or_disabled"),
    ("disabled", "existing_filter_hidden_or_disabled"), ("observed_only", "ambiguous_filter_match"),
])
def test_reuse_blocks_ambiguous_hidden_or_unverified_matches(reader, scenario, expected):
    mock_rules(reader)
    rows = [candidate(101, parameter("String", "North"), "North")]
    existing, applied = [existing_filter(201, "North")], []
    if scenario == "duplicate":
        existing.append(existing_filter(202, "North"))
    elif scenario == "observed_only":
        existing = [existing_filter(201, "NORTH")]
    else:
        applied = [(201, (scenario != "hidden", scenario != "disabled"))]
    match = reuse_by_value(qualifying_inventory(reader, rows, existing=existing, applied=applied,
                                                reuse=True))["North"]
    assert match["status"] == "blocked" and match["reason"] == expected and match["selected_id"] is None
    if scenario == "observed_only":
        assert match["exact"] == [] and match["uncertain_filter_ids"] == ["201"]


def existing_graphics(rgb):
    from view_color_preview import COLOR_FIELDS

    result = {name: list(rgb) for name in COLOR_FIELDS}
    result.update(IsSurfaceForegroundPatternVisible=True, IsCutForegroundPatternVisible=True)
    return result


def test_color_proposal_keeps_existing_color_and_binds_matching_legend(reader, tmp_path):
    import subprocess
    from view_color_preview import build, color_text

    mock_rules(reader)
    rows = [candidate(101, parameter("String", "North"), "North"),
            candidate(102, parameter("String", "South"), "South")]
    inventory = qualifying_inventory(reader, rows, existing=[existing_filter(201, "North")],
                                      applied=[(201, (True, True))], reuse=True)
    inventory["link_candidate_ids"] = []
    match = reuse_by_value(inventory)["North"]["exact"][0]
    match["graphics"] = existing_graphics([30, 90, 160])
    proposal, preview = build(inventory)
    assert not proposal["blocked"] and preview["review_only"] and preview["steps"] == []
    legend = {row["label"]: row for row in proposal["legend_rows"]}
    assert legend["North"]["rgb"] == [30, 90, 160] and legend["North"]["filter_id"] == "201"
    assert legend["South"]["filter_id"] is None
    assert len({tuple(row["rgb"]) for row in legend.values()}) == 2
    assert [(row[1], row[5]) for row in preview["after"]] == [
        (row["label"], color_text(row["rgb"])) for row in proposal["legend_rows"]
    ]

    file = tmp_path / "native-inventory.json"
    file.write_text(json.dumps({"result": {"status": "ok", "inventory_json": json.dumps(inventory)}}))
    result = subprocess.run([sys.executable, str(SCRIPTS / "view_color_preview.py"), str(file)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"proposal": proposal, "preview": preview}


def test_color_preview_disambiguates_equal_display_labels_and_fails_on_name_collision(reader):
    from view_color_preview import build

    mock_rules(reader)
    rows = [candidate(101, parameter("Integer", 4), 4), candidate(102, parameter("Integer", 5), 5)]
    inventory = qualifying_inventory(reader, rows, reuse=True)
    inventory["link_candidate_ids"] = []
    proposal, _ = build(inventory)
    legend = proposal["legend_rows"]
    assert {row["label"] for row in legend} == {"label [Integer 4]", "label [Integer 5]"}
    inventory["existing_filters"]["existing_parameter_filter_names"].append(
        {"id": "201", "name": legend[0]["filter_name"].lower()}
    )
    blocked, preview = build(inventory)
    assert blocked["blocked"]
    assert preview["warnings"] == [{
        "row": legend[0]["value_key"], "text": "The proposed filter name is already in use.", "blocking": True,
    }]


def test_color_preview_flags_link_scope_and_conflicting_applied_colors(reader):
    from view_color_preview import build

    mock_rules(reader)
    rows = [candidate(101, parameter("String", "North"), "North"),
            candidate(102, parameter("String", "South"), "South")]
    inventory = qualifying_inventory(reader, rows, existing=[existing_filter(201, "North"), existing_filter(202, "South")],
                                      applied=[(201, (True, True)), (202, (True, True))], reuse=True)
    for group in reuse_by_value(inventory).values():
        group["exact"][0]["graphics"] = existing_graphics([30, 90, 160])
    proposal, preview = build(inventory)
    assert proposal["blocked"]
    assert len({tuple(row["rgb"]) for row in proposal["legend_rows"]}) == 2
    assert len(preview["warnings"]) == 2
    assert all(warning["blocking"] for warning in preview["warnings"])


def test_color_preview_rejects_duplicate_element_membership(reader):
    from view_color_preview import build

    mock_rules(reader)
    rows = [candidate(101, parameter("String", "North"), "North"),
            candidate(102, parameter("String", "South"), "South")]
    inventory = qualifying_inventory(reader, rows, reuse=True)
    groups = inventory["native_rules"]["groups"]
    groups[1]["member_ids"] = list(groups[0]["member_ids"])
    with pytest.raises(ValueError, match="every observed element exactly once"):
        build(inventory)
