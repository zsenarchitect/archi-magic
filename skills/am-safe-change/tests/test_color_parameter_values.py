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
    Name = "Study"
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
        if kind is Link:
            return [Link()]
        if kind is View:
            return self.document.views
        return self.document.existing_filters

    def OwnedByView(self, identifier):
        assert identifier.Value == 41
        return NS(WhereElementIsNotElementType=lambda: self.document.legend_contents)

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
        identifier=ElementId, key=lambda value: str(value.Value),
        can_edit=lambda _state: True, owner_state=lambda _doc, _view: None,
        viewports=lambda doc, ids, _clock: {item: doc.placements.get(item, []) for item in ids}))
    monkeypatch.setitem(sys.modules, "read_view_filter_graphics", NS(native_number=float, rule_signature=lambda rule, *_: dict(rule.signature),
                                                      graphics=lambda settings, _solid: settings.values,
                                                      color=lambda value: value,
                                                      pattern_definition=lambda doc, pattern_id, *_: doc.patterns[pattern_id],
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


def native_legend_fixture(reader, monkeypatch, existing=None):
    from view_color_preview import legend_name, workflow_identity

    class NativeType:
        def __init__(self, identifier, name):
            self.Id = ElementId(identifier)
            self.Name = name
            self.UniqueId = "uid-%d" % identifier
            self.VersionGuid = "version-%d" % identifier

    class DraftingType(NativeType):
        ViewFamily = "Drafting"
        DefaultTemplateId = ElementId(-1)

    class RegionType(NativeType):
        ForegroundPatternId = ElementId(74)
        BackgroundPatternId = ElementId(-1)
        ForegroundPatternColor = [0, 0, 0]
        BackgroundPatternColor = None
        IsMasking = False
        LineWeight = 2

    class TextType(NativeType):
        def get_Parameter(self, parameter_id):
            assert parameter_id == "TEXT_SIZE"
            return NS(HasValue=True, AsDouble=lambda: 0.01)

    class PatternType(NativeType):
        pass

    class DraftingView(View):
        Id = ElementId(41)
        UniqueId = "legend-41"
        VersionGuid = "legend-version"
        ViewTemplateId = ElementId(-1)
        Scale = 1

        def GetTypeId(self):
            return ElementId(71)

        def GetPlacementOnSheetStatus(self):
            return "CompletelyPlaced"

    class Note:
        Id = ElementId(151)
        UniqueId = "note-151"
        OwnerViewId = ElementId(41)
        Text = "Existing North label"
        Coord = NS(X=0.1, Y=0.2, Z=0)

        def GetTypeId(self):
            return ElementId(73)

        def GetType(self):
            return NS(Name="TextNote")

        def GetLeaders(self):
            return []

    db = reader["DB"]
    db.ViewDrafting, db.FilledRegion, db.TextNote = DraftingView, type("FilledRegion", (), {}), Note
    db.ViewFamilyType, db.FilledRegionType, db.TextNoteType, db.FillPatternElement = (
        DraftingType, RegionType, TextType, PatternType)
    db.Element = NS(Name=NS(GetValue=lambda item: item.Name))
    db.ViewFamily = NS(Drafting="Drafting")
    db.BuiltInParameter = NS(TEXT_SIZE="TEXT_SIZE")
    module = runpy.run_path(str(SCRIPTS / "read_color_legend.py"))
    monkeypatch.setitem(sys.modules, "read_color_legend", NS(**module))

    values = {31: View(), 71: DraftingType(71, "Drafting"), 72: RegionType(72, "Swatch region"),
              73: TextType(73, "Legend text"), 74: PatternType(74, "Solid")}
    source = {"document": {"project_uid": "project-1"}, "view": {"uid": "view-31"}, "parameter_id": "-55",
              "category_ids": ["-200"]}
    expected_name = legend_name(workflow_identity(source))
    views = [values[31]]
    if existing:
        view = DraftingView()
        view.Name = expected_name.lower() if existing == "collision" else expected_name
        views.append(view)
    candidate_row = candidate(101, parameter("String", "North"), "North")
    values[101] = candidate_row
    document = NS(GetElement=lambda identifier: values.get(identifier.Value), collectors_closed=0,
                  views=views, patterns={"74": {"kind": "fill", "uid": "uid-74", "solid": True}},
                  placements={"41": [["200", "sheet-200", "A-101", "201", "viewport-201"]]},
                  legend_contents=[Note()], candidates=[candidate_row], existing_filters=[])
    args = {"view_id": "31", "parameter_id": "-55", "category_ids": ["-200"],
            "check_rules": True, "check_reuse": True,
            "legend_types": {"drafting_type_id": "71", "region_type_id": "72",
                             "text_type_id": "73", "solid_pattern_id": "74"}}
    return document, args, expected_name, values


def test_native_legend_resources_bind_exact_types_and_preview_name(reader, monkeypatch):
    from view_color_preview import build

    mock_rules(reader)
    document, args, expected_name, _ = native_legend_fixture(reader, monkeypatch)
    inventory = json.loads(reader["main"](document, args)["inventory_json"])
    preflight = inventory["legend_preflight"]
    assert preflight["name"] == expected_name and preflight["state"] == "available"
    assert {kind: row["uid"] for kind, row in preflight["resources"].items()} == {
        "drafting": "uid-71", "region": "uid-72", "text": "uid-73", "solid": "uid-74",
    }
    assert preflight["resources"]["text"]["text_size_ft"] == 0.01
    proposal, _ = build(inventory)
    assert proposal["legend_name"] == expected_name and proposal["legend_preflight"] == preflight


@pytest.mark.parametrize("existing,expected", [
    ("collision", "name_conflict"), ("draft", "existing_requires_verification"),
])
def test_native_legend_preserves_collisions_and_records_existing_contents(reader, monkeypatch, existing, expected):
    from view_color_preview import build

    mock_rules(reader)
    document, args, _, _ = native_legend_fixture(reader, monkeypatch, existing)
    inventory = json.loads(reader["main"](document, args)["inventory_json"])
    preflight = inventory["legend_preflight"]
    assert preflight["state"] == expected and preflight["existing"]["uid"] == "legend-41"
    if existing == "draft":
        assert preflight["existing"]["placements"] == [["200", "sheet-200", "A-101", "201", "viewport-201"]]
        assert preflight["existing"]["contents"] == [
            {"id": "151", "uid": "note-151", "owner_id": "41", "type_id": "73", "native_kind": "TextNote",
             "kind": "note", "text": "Existing North label", "position": [0.1, 0.2, 0.0], "leaders": 0},
        ]
    proposal, display = build(inventory)
    assert proposal["blocked"] and any(expected in item["text"] for item in display["warnings"])


def test_native_legend_refuses_non_solid_pattern_and_reports_template_default(reader, monkeypatch):
    mock_rules(reader)
    document, args, _, values = native_legend_fixture(reader, monkeypatch)
    values[71].DefaultTemplateId = ElementId(81)
    preflight = json.loads(reader["main"](document, args)["inventory_json"])["legend_preflight"]
    assert preflight["state"] == "default_template_requires_verification"
    assert preflight["resources"]["drafting"]["default_template_id"] == "81"
    document.patterns["74"]["solid"] = False
    with pytest.raises(ValueError, match="solid fill pattern"):
        reader["main"](document, args)


@pytest.mark.parametrize("fault", [None, "swatch", "extra_element"])
def test_legend_trial_reads_created_swatch_and_rolls_back(reader, monkeypatch, fault):
    from view_color_preview import build

    mock_rules(reader)
    document, request, _, _ = native_legend_fixture(reader, monkeypatch)
    db = reader["DB"]
    baseline = json.loads(reader["main"](document, request)["inventory_json"])
    baseline["link_candidate_ids"] = []
    proposal, display = build(baseline)
    assert not proposal["blocked"]
    assert proposal["legend_rows"][0]["swatch_bounds_ft"] == [0, -0.015, 0.05, 0.0]

    helper_module = sys.modules["duplicate_views"]
    helpers = runpy.run_path(str(SCRIPTS / "duplicate_views.py"))
    helper_module.capture_changes = helpers["capture_changes"]
    helper_module.rollback = helpers["rollback"]
    monkeypatch.setitem(sys.modules, "read_color_parameter_values",
                        NS(main=lambda *_: {"inventory_json": json.dumps(baseline)}))
    trial = runpy.run_path(str(SCRIPTS / "probe_color_legend.py"))

    class Vec:
        def __init__(self, x, y, z):
            self.X, self.Y, self.Z = x, y, z

        def Add(self, other):
            return Vec(self.X + other.X, self.Y + other.Y, self.Z + other.Z)

        def Multiply(self, scale):
            return Vec(self.X * scale, self.Y * scale, self.Z * scale)

        def Subtract(self, other):
            return self.Add(other.Multiply(-1))

        def DotProduct(self, other):
            return self.X * other.X + self.Y * other.Y + self.Z * other.Z

    class Line:
        IsBound = True

        def __init__(self, start, end):
            self.ends = start, end

        def GetEndPoint(self, index):
            return self.ends[index]

        CreateBound = staticmethod(lambda start, end: Line(start, end))

    class Loop(list):
        Append = list.append
        Dispose = lambda self: None

    class Boundaries:
        def __class_getitem__(cls, _kind):
            return cls

        def Add(self, item):
            self.loop = item

    class Settings:
        def __init__(self):
            self.values = {}

        def SetSurfaceForegroundPatternId(self, identifier):
            self.values["SurfaceForegroundPatternId"] = {"kind": "element", "id": str(identifier.Value)}

        def SetSurfaceForegroundPatternColor(self, color):
            self.values["SurfaceForegroundPatternColor"] = list(color)

        def SetSurfaceForegroundPatternVisible(self, value):
            self.values["IsSurfaceForegroundPatternVisible"] = value

        def SetSurfaceBackgroundPatternVisible(self, value):
            self.values["IsSurfaceBackgroundPatternVisible"] = value

        def Dispose(self):
            pass

    original_get = document.GetElement
    created = {}
    settings = {}
    document.GetElement = lambda identifier: created.get(identifier.Value) or original_get(identifier)

    class Region:
        def __init__(self, boundaries):
            self.Id, self.UniqueId, self.OwnerViewId = ElementId(501), "region-501", ElementId(41)
            self.loops = [list(boundaries.loop)]

        def GetType(self):
            return NS(Name="FilledRegion")

        def GetTypeId(self):
            return ElementId(72)

        def GetBoundaries(self):
            return self.loops

        @staticmethod
        def Create(doc, type_id, view_id, boundaries):
            assert doc is document and (type_id.Value, view_id.Value) == (72, 41)
            result = Region(boundaries)
            created[501] = result
            document.legend_contents.append(result)
            return result

    class Note:
        def __init__(self, position, text):
            self.Id, self.UniqueId, self.OwnerViewId = ElementId(502), "note-502", ElementId(41)
            self.Coord, self.Text = position, text

        def GetType(self):
            return NS(Name="TextNote")

        def GetTypeId(self):
            return ElementId(73)

        def GetLeaders(self):
            return []

        def get_BoundingBox(self, _view):
            return NS(Min=self.Coord.Add(Vec(0, -0.01, 0)), Max=self.Coord.Add(Vec(0.1, 0, 0)),
                      Transform=NS(OfPoint=lambda value: value))

        @staticmethod
        def Create(doc, view_id, position, text, type_id):
            assert doc is document and (view_id.Value, type_id.Value) == (41, 73)
            result = Note(position, text)
            created[502] = result
            document.legend_contents.append(result)
            return result

    def create_view(doc, type_id):
        assert doc is document and type_id.Value == 71
        view = db.ViewDrafting()
        view.Origin, view.RightDirection, view.UpDirection = Vec(0, 0, 0), Vec(1, 0, 0), Vec(0, 1, 0)
        view.SetElementOverrides = lambda identifier, override: settings.__setitem__(identifier.Value, override)
        view.GetElementOverrides = lambda identifier: settings[identifier.Value]
        created[41] = view
        return view

    class Handlers:
        def __iadd__(self, callback):
            self.callback = callback
            return self

        def __isub__(self, callback):
            assert self.callback is callback
            self.callback = None
            return self

    class Collection(list):
        Count = property(len)

    class Group:
        def __init__(self, *_):
            self.status = "Uninitialized"

        def Start(self):
            self.status = "Started"
            return self.status

        def GetStatus(self):
            return self.status

        def RollBack(self):
            created.clear()
            document.legend_contents.clear()
            self.status = "RolledBack"
            return self.status

    class Transaction(Group):
        def Commit(self):
            self.status = "Committed"
            if fault == "swatch":
                settings[501].values["SurfaceForegroundPatternColor"] = [0, 0, 0]
            if fault == "extra_element":
                created[999] = NS()
            event = NS(GetDocument=lambda: document, GetTransactionNames=lambda: [trial["TRANSACTION_NAME"]],
                       GetAddedElementIds=lambda: Collection(ElementId(value) for value in created),
                       GetDeletedElementIds=Collection, GetModifiedElementIds=Collection)
            document.Application.DocumentChanged.callback(None, event)
            return self.status

    document.legend_contents = []
    document.IsWorkshared = False
    document.Application = NS(DocumentChanged=Handlers())
    document.Equals = lambda other: other is document
    document.Regenerate = lambda: None
    db.ViewDrafting.Create = staticmethod(create_view)
    db.XYZ, db.Line, db.CurveLoop, db.FilledRegion, db.TextNote = Vec, Line, Loop, Region, Note
    db.OverrideGraphicSettings, db.Color = Settings, lambda *rgb: rgb
    db.TransactionGroup, db.Transaction = Group, Transaction
    db.TransactionStatus = NS(Started="Started", Pending="Pending", RolledBack="RolledBack", Committed="Committed")
    trial["probe"].__globals__["List"] = Boundaries

    result = trial["probe"](document, {"disposable_fixture": True, "request": request,
                                       "proposal": proposal, "preview": display})
    if fault:
        assert result["status"] == "stopped_and_rolled_back", result
        expected = "reviewed color" if fault == "swatch" else "complete transaction change record"
        assert expected in result["message"]
    else:
        assert result["status"] == "legend_trial_rolled_back", result
        assert result["receipt"]["changes"] == {"added": ["41", "501", "502"], "modified": [], "deleted": []}
        assert result["receipt"]["rows"][0]["rgb"] == proposal["legend_rows"][0]["rgb"]
        assert result["receipt"]["rows"][0]["region_uid"] == "region-501"
    assert not created and not document.legend_contents
