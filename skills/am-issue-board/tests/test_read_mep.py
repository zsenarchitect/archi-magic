import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest
from issue_board_fixtures import FakeFormattedText, FakeRange, SAMPLE


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "read_mep.py"
sys.path.insert(0, str(SCRIPT.parent))


class ElementId:
    def __init__(self, value):
        self.Value = value


class Enum:
    OST_DuctCurves = -1
    OST_FlexDuctCurves = -2
    OST_DuctFitting = -3
    OST_DuctTerminal = -4
    OST_PipeCurves = -5
    OST_FlexPipeCurves = -6
    OST_PipeFitting = -7
    OST_MechanicalEquipment = -8
    OST_Conduit = -9
    OST_ConduitFitting = -10
    OST_CableTray = -11
    OST_CableTrayFitting = -12


class NativeList(list):
    def __class_getitem__(cls, _):
        return cls

    def Add(self, value):
        self.append(value)


def point(x, y, z):
    return NS(X=x, Y=y, Z=z)


def transform(offset):
    return NS(
        Origin=point(offset, 0, 0), BasisX=point(1, 0, 0),
        BasisY=point(0, 1, 0), BasisZ=point(0, 0, 1),
        OfPoint=lambda p: point(p.X + offset, p.Y, p.Z),
        OfVector=lambda p: point(p.X, p.Y, p.Z),
    )


def model(name, elements):
    return NS(
        IsFamilyDocument=False, ProjectInformation=NS(UniqueId=name),
        PathName="C:/Tests/" + name + ".rvt", Application=NS(VersionNumber="2026"),
        GetElement=lambda identifier: elements.get(identifier.Value),
        ids=sorted(elements, reverse=True),
    )


class Link:
    UniqueId = "linked-instance"

    def __init__(self, document, offset):
        self.document = document
        self.offset = offset

    def GetLinkDocument(self):
        return self.document

    def GetTotalTransform(self):
        return transform(self.offset)


class Duct:
    def __init__(self, connectors, flow=0):
        self.Category = NS(Id=ElementId(Enum.OST_DuctCurves))
        self.UniqueId = "duct-uid"
        self.ConnectorManager = NS(Connectors=connectors)
        self.flow = flow

    def get_Parameter(self, identifier):
        spec = "airflow" if identifier == -1013405 else "duct-size"
        value = self.flow if identifier == -1013405 else 1.5
        return NS(
            Id=ElementId(identifier), StorageType="double", HasValue=True, AsDouble=lambda: value,
            Definition=NS(GetDataType=lambda: NS(Equals=lambda actual: actual == spec)),
        )


class Logical:
    ConnectorType = "Logical"
    connection_reads = 0

    @property
    def IsConnected(self):
        self.connection_reads += 1
        raise AssertionError("Logical connectors have no physical connection state")


class Collector:
    def __init__(self, document):
        self.identifiers = iter(document.ids)
        self.current = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def WherePasses(self, categories):
        assert set(categories) == {value for name, value in vars(Enum).items() if name.startswith("OST_")}
        return self

    def WhereElementIsNotElementType(self):
        return self

    def GetElementIdIterator(self):
        return self

    def MoveNext(self):
        self.current = next(self.identifiers, None)
        return self.current is not None

    @property
    def Current(self):
        return ElementId(self.current)


@pytest.fixture
def runtime(monkeypatch):
    db = NS(
        BuiltInCategory=Enum, ElementId=ElementId, XYZ=point, ElementType=type("ElementType", (), {}),
        MEPCurve=Duct, RevitLinkInstance=Link, Transform=NS(Identity=transform(0)),
        FilteredElementCollector=Collector, ElementMulticategoryFilter=lambda values: values,
        BuiltInParameter=NS(RBS_CURVE_DIAMETER_PARAM=-1114103, RBS_DUCT_FLOW_PARAM=-1013405),
        SpecTypeId=NS(DuctSize="duct-size", AirFlow="airflow"), StorageType=NS(Double="double"),
    )
    modules = {
        "System": NS(Guid=str, Int64=int),
        "System.Collections.Generic": NS(List=NativeList),
        "System.Diagnostics": NS(Stopwatch=NS(StartNew=lambda: NS(ElapsedMilliseconds=0))),
        "pyrevit.api": NS(DB=db),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return db


def execute(document, **request):
    return runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": request})["result"]


def connector(identifier, *, connected, system):
    return NS(Id=identifier, ConnectorType="End", Shape="Round", Domain="DomainHvac",
              IsConnected=connected, Origin=point(identifier, 2, 3), MEPSystem=system)


@pytest.mark.parametrize("scenario", [
    "host", "rotated_link", "connected", "duplicate", "wrong_uid", "invalid_frame", "moved_endpoint", "oval",
])
def test_read_reviewed_route_endpoints(runtime, scenario):
    first = connector(1, connected=scenario == "connected", system=None)
    second = connector(2, connected=False, system=None)
    for item in (first, second):
        item.CoordinateSystem = transform(0)
        item.CoordinateSystem.Origin = item.Origin
        item.Radius = 0.25
    second.Shape = "Oval" if scenario == "oval" else "Rectangular"
    second.Width, second.Height = 0.75, 0.5
    if scenario == "invalid_frame":
        second.CoordinateSystem.BasisY = point(1, 0, 0)
    source_doc = model("source", {42: Duct([first]), 43: Duct([second])})
    host, link_request = source_doc, {}
    if scenario == "rotated_link":
        link = Link(source_doc, 10)
        link.GetTotalTransform = lambda: NS(
            Origin=point(10, 20, 30), BasisX=point(0, 1, 0), BasisY=point(-1, 0, 0),
            BasisZ=point(0, 0, 1), OfVector=lambda value: point(-value.Y, value.X, value.Z),
            OfPoint=lambda value: point(10 - value.Y, 20 + value.X, 30 + value.Z),
        )
        host = model("host", {41: link})
        link_request = {"link_instance_id": "41", "link_instance_uid": link.UniqueId}
    endpoints = [
        {"element_id": "42", "element_uid": "duct-uid", "connector_id": 1},
        {"element_id": "43", "element_uid": "missing" if scenario == "wrong_uid" else "duct-uid",
         "connector_id": 2},
    ]
    if scenario == "duplicate":
        endpoints[1] = dict(endpoints[0])
    if scenario == "moved_endpoint":
        original = source_doc.GetElement
        reads = iter([2, 3])

        def moved(identifier):
            if identifier.Value == 43:
                second.Origin = point(next(reads), 2, 3)
                second.CoordinateSystem.Origin = second.Origin
            return original(identifier)

        source_doc.GetElement = moved
    identity = execute(host, describe=True, **link_request)["source"]
    observed = execute(host, source=identity, route_endpoints=endpoints, **link_request)
    failures = {
        "connected": "not a physical end", "duplicate": "two distinct",
        "wrong_uid": "missing or changed", "invalid_frame": "invalid coordinate frame",
        "moved_endpoint": "changed while being read",
    }
    if scenario in failures:
        assert observed["status"] == "incomplete" and failures[scenario] in observed["message"]
        assert "endpoints_json" not in observed
        return
    assert observed["status"] == "ok" and "snapshot_json" not in observed
    result = json.loads(observed["endpoints_json"])
    assert (result["kind"], result["version"], result["source"]) == ("mep-route-endpoints", 1, identity)
    a, b = result["endpoints"]
    assert a["dimensions_ft"] == [0.5] and a["domain"] == "DomainHvac"
    assert b["dimensions_ft"] == (None if scenario == "oval" else [0.75, 0.5])
    if scenario == "rotated_link":
        assert (a["origin_ft"], b["origin_ft"]) == ([8.0, 21.0, 33.0], [8.0, 22.0, 33.0])
        assert a["axes"] == [[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
    else:
        assert a["origin_ft"] == [1.0, 2.0, 3.0] and a["axes"][2] == [0.0, 0.0, 1.0]


@pytest.mark.parametrize("scenario", ["duct", "pipe", "no_match", "changed", "wrong_type", "missing_symbol"])
def test_route_elbow_uses_the_reviewed_type_and_both_end_sizes(runtime, scenario):
    class Resource:
        def __init__(self, **fields):
            self.__dict__.update(fields)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class DuctType:
        UniqueId = "curve-type"
        Shape = "Round"

    class PipeType:
        UniqueId = "curve-type"

    class FamilySymbol:
        UniqueId = "elbow-type"

    runtime.Mechanical, runtime.Plumbing = NS(DuctType=DuctType), NS(PipeType=PipeType)
    runtime.FamilySymbol = FamilySymbol
    runtime.RoutingPreferenceErrorLevel = NS(**{"None": "none"})
    runtime.RoutingPreferenceRuleGroupType = NS(Elbows="elbows")
    runtime.ElementId.InvalidElementId = ElementId(-1)
    observed = []

    def conditions(error_level):
        assert error_level == "none"
        values = []
        return Resource(values=values, AppendCondition=lambda item: values.append(item.diameter))

    def get_part(group, conditions):
        assert group == "elbows" and conditions.values == [0.5, 0.5]
        observed.append(conditions.values)
        absent = scenario == "no_match" or (scenario == "changed" and len(observed) == 2)
        return ElementId(-1 if absent else 43)

    runtime.RoutingCondition = lambda diameter: Resource(diameter=diameter)
    runtime.RoutingConditions = conditions
    curve_type = PipeType() if scenario == "pipe" else DuctType()
    curve_type.RoutingPreferenceManager = Resource(OwnerId=ElementId(42), GetMEPPartId=get_part)
    if scenario == "wrong_type":
        curve_type.Shape = "Rectangular"
    host = model("host", {42: curve_type, 43: None if scenario == "missing_symbol" else FamilySymbol()})
    identity = execute(host, describe=True)["source"]
    reviewed = {"curve_type_id": "42", "curve_type_uid": "curve-type", "diameter_ft": 0.5}
    result = execute(host, source=identity, route_elbow=reviewed)
    errors = {"changed": "changed while being read", "wrong_type": "round duct or pipe",
              "missing_symbol": "unresolved routing preference"}
    if scenario in errors:
        assert result["status"] == "incomplete" and errors[scenario] in result["message"]
        assert "elbow_json" not in result
        return
    assert observed == [[0.5, 0.5], [0.5, 0.5]]
    expected = None if scenario == "no_match" else {"id": "43", "uid": "elbow-type"}
    assert json.loads(result["elbow_json"]) == {
        "kind": "mep-route-elbow", "version": 1, "source": identity,
        "fitting": dict(reviewed, domain="DomainPiping" if scenario == "pipe" else "DomainHvac", elbow=expected),
    }


@pytest.mark.parametrize("scenario", ["host", "rotated_link", "wrong_uid", "missing_box", "moved"])
def test_read_reviewed_obstacle_boxes(runtime, scenario):
    low, high = point(1, 2, 3), point(4, 6, 8)
    observed_boxes = iter([NS(Min=low, Max=high), NS(Min=point(2, 2, 3), Max=high)])
    def bounds(view):
        assert view is None
        if scenario == "missing_box":
            return None
        if scenario == "moved":
            return next(observed_boxes)
        return NS(Min=low, Max=high)

    source_doc = model("obstacles", {42: NS(UniqueId="wall-uid", get_BoundingBox=bounds)})
    host, link_request = source_doc, {}
    if scenario == "rotated_link":
        link = Link(source_doc, 10)
        link.GetTotalTransform = lambda: NS(
            Origin=point(10, 20, 30), BasisX=point(0, 1, 0), BasisY=point(-1, 0, 0),
            BasisZ=point(0, 0, 1), OfPoint=lambda value: point(10 - value.Y, 20 + value.X, 30 + value.Z),
        )
        host = model("host", {41: link})
        link_request = {"link_instance_id": "41", "link_instance_uid": link.UniqueId}
    identity = execute(host, describe=True, **link_request)["source"]
    selected = {"element_id": "42", "element_uid": "stale" if scenario == "wrong_uid" else "wall-uid"}
    result = execute(host, source=identity, route_obstacles=[selected], **link_request)
    errors = {"wrong_uid": "changed identity", "missing_box": "no model bounding box",
              "moved": "changed while being read"}
    if scenario in errors:
        assert result["status"] == "incomplete" and errors[scenario] in result["message"]
        assert "obstacles_json" not in result
        return
    low, high = ([4, 21, 33], [8, 24, 38]) if scenario == "rotated_link" else ([1, 2, 3], [4, 6, 8])
    assert json.loads(result["obstacles_json"]) == {
        "kind": "mep-route-obstacles", "version": 1, "source": identity,
        "obstacles": [dict(selected, min_ft=low, max_ft=high)],
    }


def test_link_query_uses_source_ids_transforms_and_native_flow(runtime):
    system = NS(Id=ElementId(9), Name="SA-01", GetTypeId=lambda: ElementId(10))
    logical = Logical()
    duct = Duct([connector(2, connected=True, system=system), logical,
                 connector(1, connected=False, system=system)])
    link_document = model("linked", {
        42: duct, 10: NS(SystemClassification="SupplyAir", CalculationLevel="Flow"),
    })
    link = Link(link_document, 50)
    host = model("host", {41: link, 42: None})
    link_request = {"link_instance_id": "41", "link_instance_uid": link.UniqueId}
    source = execute(host, describe=True, **link_request)["source"]
    result = execute(host, source=source, element_ids=["42"], **link_request)
    payload = json.loads(result["snapshot_json"])
    assert set(payload) == {"kind", "version", "snapshot"}
    snapshot = payload["snapshot"]
    assert snapshot["source"]["host_identity"] != snapshot["source"]["document_identity"]
    assert snapshot["coverage"] == {"elements": 1, "connectors_visited": 3, "connectors_retained": 2}
    row, = snapshot["elements"]
    assert (row["id"], row["uid"]) == ("42", "duct-uid")
    assert row["sizes"] == {"Diameter": {"state": "observed", "value": 1.5}}
    assert row["airflow"] == {"state": "observed", "value": 0.0}
    assert row["systems"] == [{"id": "9", "name": "SA-01", "classification": "SupplyAir",
                               "calculation": "Flow"}]
    assert [(item["id"], item["isConnected"], item["origin"]["x"]) for item in row["connectors"]] == [
        (1, False, 51.0), (2, True, 52.0),
    ]
    assert logical.connection_reads == 0


@pytest.mark.parametrize("classification,mode,expected", [
    ("ReturnHydronic", "All", "not_applicable"),
    ("ReturnAir", "Performance", "not_applicable"),
    ("ReturnAir", "Flow", "observed"),
])
def test_flow_requires_exact_air_class_and_calculation(runtime, classification, mode, expected):
    system = NS(Id=ElementId(9), Name="RA-01", GetTypeId=lambda: ElementId(10))
    host = model("host", {10: NS(SystemClassification=classification, CalculationLevel=mode),
                          42: Duct([connector(1, connected=True, system=system)])})
    source = execute(host, describe=True)["source"]
    result = execute(host, source=source, element_ids=["42"])
    row, = json.loads(result["snapshot_json"])["snapshot"]["elements"]
    assert row["airflow"]["state"] == expected


def test_link_move_and_unresolved_ids_do_not_produce_a_snapshot(runtime):
    link = Link(model("linked", {}), 50)
    host = model("host", {41: link})
    link_request = {"link_instance_id": "41", "link_instance_uid": link.UniqueId}
    source = execute(host, describe=True, **link_request)["source"]
    missing = execute(host, source=source, element_ids=["42"], **link_request)
    assert missing["status"] == "incomplete" and "supported MEP instance" in missing["message"]
    link.offset = 51
    moved = execute(host, source=source, element_ids=["42"], **link_request)
    assert moved["status"] == "incomplete" and "differs from the reviewed" in moved["message"]
    assert "snapshot_json" not in missing and "snapshot_json" not in moved


def test_connector_limits_keep_an_incomplete_scope_out_of_the_audit(runtime):
    duct = Duct([connector(index, connected=True, system=None) for index in range(33)])
    host = model("host", {42: duct})
    result = execute(host, source=execute(host, describe=True)["source"], element_ids=["42"])
    assert result["status"] == "incomplete" and "connector limit" in result["message"]
    assert result["coverage"] == {"elements": 0, "connectors_visited": 33, "connectors_retained": 32}
    assert "snapshot_json" not in result


def test_category_discovery_is_bounded_and_distinguishes_complete_empty(runtime):
    host = model("host", {})
    source = execute(host, describe=True)["source"]
    empty = execute(host, source=source)
    snapshot = json.loads(empty["snapshot_json"])["snapshot"]
    assert snapshot["status"] == "complete" and snapshot["elements"] == []

    visits = []

    def many_ids():
        for index in range(1, 503):
            visits.append(index)
            yield index

    host.ids = many_ids()
    capped = execute(host, source=source)
    assert capped["status"] == "incomplete" and "exceeds 500 elements" in capped["message"]
    assert len(visits) == 501 and "snapshot_json" not in capped


@pytest.mark.parametrize("scenario,exit_code,status,rules", [
    ("clean", 0, "audited", []),
    ("seeded", 1, "audited", ["MEP-CON-01", "MEP-SYS-01", "MEP-FLOW-01"]),
    ("partial", 3, "incomplete_evaluation", ["MEP-CON-01", "MEP-SYS-01"]),
    ("empty", 0, "empty_scope", []),
    ("mixed", 2, None, []),
    ("wrong_count", 2, None, []),
    ("duplicate_uid", 2, None, []),
    ("incomplete_inventory", 2, None, []),
])
def test_native_snapshot_to_audit_cli(runtime, tmp_path, scenario, exit_code, status, rules):
    system = NS(Id=ElementId(9), Name="SA-01" if scenario == "clean" else "BAD-01",
                GetTypeId=lambda: ElementId(10))
    connectors = [connector(4, connected=scenario == "clean", system=system)]
    if scenario == "partial":
        connectors.append(NS(ConnectorType="EndSurface"))
    host = model("host", {
        10: NS(SystemClassification="SupplyAir", CalculationLevel="Flow"),
        42: Duct(connectors, flow=120 if scenario == "clean" else 0),
    })
    if scenario == "empty":
        host.ids = []
    source = execute(host, describe=True)["source"]
    request = {} if scenario == "empty" else {"element_ids": ["42"]}
    snapshot = json.loads(execute(host, source=source, **request)["snapshot_json"])
    if scenario == "mixed":
        snapshot["elements"] = snapshot["snapshot"]["elements"]
    elif scenario == "wrong_count":
        snapshot["snapshot"]["coverage"]["connectors_retained"] += 1
    elif scenario == "duplicate_uid":
        payload = snapshot["snapshot"]
        payload["elements"].append(dict(payload["elements"][0], id="43"))
        for key in payload["coverage"]:
            payload["coverage"][key] *= 2
    elif scenario == "incomplete_inventory":
        snapshot["snapshot"]["status"] = "incomplete"
    source_path, report_path = tmp_path / "snapshot.json", tmp_path / "report.json"
    source_path.write_text(json.dumps(snapshot))
    result = subprocess.run([sys.executable, str(SCRIPT.with_name("mep_qa.py")), str(source_path),
                             "--output", str(report_path)], capture_output=True, text=True)
    assert result.returncode == exit_code, result.stderr
    if exit_code == 2:
        assert not report_path.exists()
        return
    output = json.loads(report_path.read_text())
    assert set(output) == {"kind", "version", "report"} and output["kind"] == "mep-native-report"
    report = output["report"]
    assert (report["source"], report["status"]) == (source, status)
    assert [item["ruleId"] for item in report["findings"]] == rules
    assert report["targets"] == ({"42": snapshot["snapshot"]["elements"][0]} if rules else {})
    if scenario == "partial":
        assert report["diagnostics"] == [
            {"elementId": "42", "unavailable": ["system", "connectivity", "airflow"]},
        ]
        assert report["summary"] == {"errors": 1, "warnings": 1, "infos": 0}
    if scenario == "clean":
        assert all(report["coverage"][check]["evaluated"] == 1
                   for check in ("system", "connectivity", "size", "airflow"))


def test_large_native_audit_uses_complete_runtime_pages(runtime, tmp_path):
    system = NS(Id=ElementId(9), Name="SA-01", GetTypeId=lambda: ElementId(10))
    connected = [connector(index, connected=True, system=system) for index in range(4)]
    ducts = {identifier: Duct(connected, flow=120) for identifier in range(1000, 1500)}
    for identifier, duct in ducts.items():
        duct.UniqueId = f"duct-{identifier}"
    host = model("host", {10: NS(SystemClassification="SupplyAir", CalculationLevel="Flow"), **ducts})
    request = {"source": execute(host, describe=True)["source"], "element_ids": [str(item) for item in ducts]}
    result = execute(host, **request)
    assert result["status"] == "ok", result
    assert 200 * 1024 < len(result["snapshot_json"]) <= 512 * 1024
    encoded = json.dumps(result).encode("utf-16-le")
    units = len(encoded) // 2
    response = {"run_id": "a00000000001", "mode": "query", "decision": "rolled_back", "status": "ok",
                "exit_code": 0, "changes": {"added_count": 0, "modified_count": 0, "deleted_count": 0}}
    pages = []
    for start in range(0, units, 100 * 1024):
        end = min(start + 100 * 1024, units)
        pages.append({"response": dict(response, result_truncated=True, result=None, result_page={
            "offset": start, "length": end - start, "total_length": units,
            "text": encoded[2 * start:2 * end].decode("utf-16-le", "surrogatepass"),
        })})

    source, output = tmp_path / "runtime.json", tmp_path / "report.json"
    cli = [sys.executable, str(SCRIPT.with_name("mep_qa.py")), str(source), "--runtime-record",
           "--output", str(output)]
    for record in (list(reversed(pages)), dict(response, result_truncated=False, result=result)):
        source.write_text(json.dumps(record))
        invocation = subprocess.run(cli, capture_output=True, text=True)
        assert invocation.returncode == 0, invocation.stderr
        report = json.loads(output.read_text())["report"]
        assert report["status"] == "audited" and report["findings"] == []
        assert report["inventory"] == {"elements": 500, "connectors_visited": 2000, "connectors_retained": 2000}
        assert report["coverage"]["airflow"]["evaluated"] == 500
        output.unlink()

    foreign = dict(pages[1]["response"], run_id="b00000000002")
    dirty = dict(pages[0]["response"], changes={"modified_count": 1})
    for invalid in (pages[1:], [pages[0], foreign, *pages[2:]], [dirty, *pages[1:]], [*pages, pages[0]]):
        source.write_text(json.dumps(invalid))
        invocation = subprocess.run(cli, capture_output=True, text=True)
        assert invocation.returncode == 2 and "incomplete or its pages disagree" in invocation.stderr
        assert not output.exists()


@pytest.mark.parametrize("scenario", [
    "host", "linked", "moved_link", "changed_element", "changed_evidence", "changed_note",
    "wrong_board_project", "unguarded_inner", "missing_source_guard", "unexpected_element_link",
])
def test_native_mep_handoff_through_board_writer(runtime, monkeypatch, tmp_path, scenario):
    import issue_board as ib

    events = []

    class TextNote:
        Id = ElementId(40)
        UniqueId = "board-note"

        def __init__(self):
            self.text = FakeFormattedText(SAMPLE)

        def GetFormattedText(self):
            return self.text

        def SetFormattedText(self, text):
            self.text = text
            events.append("write")

    def transaction(*_):
        events.append("transaction")
        return NS(Start=lambda: events.append("start"), Commit=lambda: events.append("commit"),
                  RollBack=lambda: events.append("rollback"))

    runtime.TextNote = TextNote
    runtime.Transaction = transaction
    runtime.TextRange = FakeRange
    runtime.ListType = NS(**{"Bullet": "bullet", "None": "none"})
    monkeypatch.delitem(sys.modules, "read_mep", raising=False)
    note = TextNote()
    system = NS(Id=ElementId(9), Name=r"BAD-id987\u0069d678", GetTypeId=lambda: ElementId(10))
    duct = Duct([connector(4, connected=False, system=system)], flow=100)
    elements = {42: duct, 10: NS(SystemClassification="SupplyAir", CalculationLevel="Flow")}
    request = {}
    if scenario in ("linked", "moved_link", "unexpected_element_link"):
        link = Link(model("link", elements), 50)
        host = model("host", {40: note, 41: link, 42: Duct([])})
        request = {"link_instance_id": "41", "link_instance_uid": link.UniqueId}
    else:
        host = model("host", {40: note, **elements})
    source = execute(host, describe=True, **request)["source"]
    snapshot = execute(host, source=source, element_ids=["42"], **request)
    snapshot_path, report_path, note_path = [tmp_path / name for name in ("snapshot.json", "report.json", "note.json")]
    snapshot_path.write_text(snapshot["snapshot_json"])
    result = subprocess.run([sys.executable, str(SCRIPT.with_name("mep_qa.py")), str(snapshot_path),
                             "--output", str(report_path)], capture_output=True, text=True)
    assert result.returncode == 1, result.stderr
    report = json.loads(report_path.read_text())["report"]
    assert len(report["findings"]) == 2
    note_path.write_text(json.dumps({
        "host_identity": ib.saved_document_identity(model("other", {})) if scenario == "wrong_board_project"
        else source["host_identity"],
        "note": {"id": "40", "unique_id": note.UniqueId, "sha1": ib.text_sha1(SAMPLE), "text": SAMPLE},
    }))
    command = [sys.executable, str(SCRIPT.with_name("board_cli.py"))]
    legacy = subprocess.run([*command, "mep-ops", str(report_path), "MEP-0002"], capture_output=True, text=True)
    assert legacy.returncode == 2 and not legacy.stdout
    preview = subprocess.run([*command, "mep-native-preview", str(report_path), str(note_path), "MEP-0002"],
                             capture_output=True, text=True)
    if scenario == "wrong_board_project":
        assert preview.returncode == 2 and not preview.stdout
        return
    assert preview.returncode == 0, preview.stderr
    plan = json.loads(preview.stdout)
    assert plan["kind"] == "mep-native-board-plan" and "kind" not in plan["edit"]
    row = ib.parse_board(plan["edit"]["final_text"])["issues"][-1]
    assert row["element_ids"] == ([] if request else [42])
    if request:
        assert row["findings"][0] == "Linked element 42 in Revit link instance 41"
    if scenario == "moved_link":
        link.offset += 1
    elif scenario == "changed_element":
        duct.UniqueId = "replacement"
    elif scenario == "changed_evidence":
        duct.flow = 150
    elif scenario == "changed_note":
        note.UniqueId = "replacement-note"
    elif scenario == "unguarded_inner":
        plan = plan["edit"]
    elif scenario == "missing_source_guard":
        plan.pop("source_guard")
    elif scenario == "unexpected_element_link":
        plan["edit"]["final_text"] = plan["edit"]["final_text"].replace("Linked element 42", "Linked element id42")
    writer_globals = {
        "__revit__": NS(ActiveUIDocument=NS(Document=host)),
        "inputs": {"plan": plan, "note_id": "40"},
    }
    if scenario in ("host", "linked"):
        result = runpy.run_path(str(SCRIPT.with_name("apply_board.py")), init_globals=writer_globals)["result"]
        assert result["status"] == "ok"
        assert events == ["transaction", "start", "write", "commit"]
        assert note.text.GetPlainText() == plan["edit"]["final_text"]
    else:
        with pytest.raises(ValueError):
            runpy.run_path(str(SCRIPT.with_name("apply_board.py")), init_globals=writer_globals)
        assert not events and note.text.GetPlainText() == SAMPLE
