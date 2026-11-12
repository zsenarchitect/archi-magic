from hashlib import sha256
import json
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class Id(int):
    Value = property(int)


Id.InvalidElementId = Id(-1)


class Line:
    IsBound = True

    def __init__(self, a, b):
        self.ends = [point(*p) for p in (a, b)]

    def GetEndPoint(self, index):
        return self.ends[index]


class LocationCurve(NS):
    def get_ElementsAtJoin(self, end):
        return self.neighbors[end]


class LocationPoint(NS):
    pass


class Level(NS):
    pass


class Wall(NS):
    pass


class FamilyInstance(NS):
    pass


class Floor(NS):
    pass


class Sketch(NS):
    pass


class PlanarFace(NS):
    pass


class Grid(NS):
    pass


class ModelLine(NS):
    pass


class DetailLine(NS):
    pass


def point(x, y, z):
    return NS(X=x, Y=y, Z=z)


def loop(vertices, z):
    return [Line((*a, z), (*b, z)) for a, b in zip(vertices, vertices[1:] + vertices[:1])]


def native(kind, identifier, **values):
    return kind(Id=Id(identifier), UniqueId=f"uid-{identifier}", **values)


def parameter(identifier, storage, value, **extra):
    return NS(Id=Id(identifier), StorageType=storage, Definition=NS(), HasValue=value is not None, IsReadOnly=False,
              IsShared="GUID" in extra, AsString=lambda: value, AsDouble=lambda: value,
              AsInteger=lambda: value, AsElementId=lambda: value, **extra)


@pytest.fixture
def model(monkeypatch):
    names = ("WALL_BASE_CONSTRAINT", "WALL_BASE_OFFSET", "WALL_HEIGHT_TYPE", "WALL_TOP_OFFSET",
             "WALL_USER_HEIGHT_PARAM", "WALL_TOP_IS_ATTACHED", "WALL_BOTTOM_IS_ATTACHED", "FAMILY_BASE_LEVEL_PARAM",
             "FAMILY_BASE_LEVEL_OFFSET_PARAM", "FAMILY_TOP_LEVEL_PARAM", "FAMILY_TOP_LEVEL_OFFSET_PARAM")
    bip = NS(**{name: name for name in names})
    db = NS(ElementId=Id, Line=Line, Level=Level, Wall=Wall, FamilyInstance=FamilyInstance, Floor=Floor,
            Sketch=Sketch, PlanarFace=PlanarFace, Grid=Grid, ModelLine=ModelLine, DetailLine=DetailLine,
            LocationCurve=LocationCurve, LocationPoint=LocationPoint,
            WallKind=NS(Basic="basic"), WallCrossSection=NS(Vertical="vertical"),
            BuiltInCategory=NS(OST_Columns=-5, OST_StructuralColumns=-6),
            BuiltInParameter=bip, StorageType=NS(String="s", Double="d", Integer="i", ElementId="e"),
            HostObjectUtils=NS(GetTopFaces=lambda floor: ["top"]),
            JoinGeometryUtils=NS(GetJoinedElements=lambda doc, element: []),
            WallUtils=NS(IsWallJoinAllowedAtEnd=lambda wall, end: end == 0))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "pyrevit", NS(api=NS(DB=db)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.syspath_prepend(str(SCRIPTS))

    params = [parameter(5, "s", "", GUID="parameter-guid"), parameter(-7, "e", Id(-8)),
              parameter(8, "s", None), parameter(9, "d", 0.12345678901234567)]
    get_bounds = lambda _: NS(Min=point(0, 0, 0), Max=point(1, 2, 20),
                              Transform=NS(OfPoint=lambda value: value, IsIdentity=True))
    source_fields = dict(ViewSpecific=False, Pinned=False, GroupId=Id(-1), DesignOption=None,
                         Parameters=params, GetTypeId=lambda: Id(100), GetDependentElements=lambda _: [])
    wall_values = {
        "WALL_BASE_CONSTRAINT": parameter(-10, "e", Id(1)), "WALL_BASE_OFFSET": parameter(-11, "d", 0),
        "WALL_HEIGHT_TYPE": parameter(-12, "e", Id(-1)), "WALL_USER_HEIGHT_PARAM": parameter(-13, "d", 20),
        "WALL_TOP_IS_ATTACHED": parameter(-14, "i", 0), "WALL_BOTTOM_IS_ATTACHED": parameter(-15, "i", 0),
    }
    wall = native(Wall, 10, WallType=NS(Kind="basic"), CrossSection="vertical", SketchId=Id(-1),
                  Orientation=point(0, -1, 0), Flipped=False, IsStackedWallMember=False,
                  Location=LocationCurve(Curve=Line((0, 0, 0), (4, 0, 0)), neighbors=[[], []]),
                  FindInserts=lambda *_: [], get_Parameter=wall_values.get, get_BoundingBox=get_bounds, **source_fields)
    column_values = {name: parameter(-20 - i, kind, value) for i, (name, kind, value) in enumerate((
        ("FAMILY_BASE_LEVEL_PARAM", "e", Id(1)), ("FAMILY_TOP_LEVEL_PARAM", "e", Id(4)),
        ("FAMILY_BASE_LEVEL_OFFSET_PARAM", "d", 0), ("FAMILY_TOP_LEVEL_OFFSET_PARAM", "d", 0)))}
    columns = [native(FamilyInstance, i, Category=NS(Id=Id(category)), CanSplit=True, IsSlantedColumn=False,
                      Symbol=NS(Family=NS(IsInPlace=False)), SuperComponent=None, Location=LocationPoint(),
                      get_Parameter=column_values.get, get_BoundingBox=get_bounds, **source_fields)
               for i, category in ((11, -5), (12, -6))]
    vertices = [(0, 0), (8, 0), (8, 4), (0, 4)]
    face = PlanarFace(FaceNormal=point(0, 0, 1), GetEdgesAsCurveLoops=lambda: [loop(vertices[::-1], -0.5)])
    floor = native(Floor, 13, SketchId=Id(20), GetSlabShapeEditor=lambda: NS(IsEnabled=False),
                   GetGeometryObjectFromReference=lambda _: face, **source_fields)
    sketch = native(Sketch, 20, OwnerId=Id(13), Profile=[loop(vertices, 0)])
    floor.GetDependentElements = lambda _: [sketch.Id]
    grid = native(Grid, 21, Curve=Line((4, 1, 0), (4, 3, 0)))
    detail_line = native(DetailLine, 22, GeometryCurve=Line((0, 2, 0), (4, 2, 0)))
    elements = [native(Level, i, ProjectElevation=z, Elevation=z + 100) for i, z in enumerate((0, 8, 15, 20), 1)]
    elements.extend([wall, *columns, floor, sketch, grid, detail_line, native(NS, 100)])
    registry = {element.Id.Value: element for element in elements}
    document = NS(IsFamilyDocument=False, IsLinked=False, IsWorkshared=False, IsModified=False, IsModifiable=False,
                  PathName="fixture.rvt", ProjectInformation=NS(UniqueId="fixture-project"),
                  Application=NS(VersionNumber="2026", ShortCurveTolerance=0.0026041666666666665),
                  GetElement=lambda identifier: registry.get(identifier.Value))
    return document, registry, face


def invoke(document, inputs):
    return runpy.run_path(str(SCRIPTS / "read_element_split_evidence.py"),
                          init_globals={"doc": document, "inputs": inputs})["result"]


def request(document):
    host = invoke(document, {"describe": True})["host_identity"]

    def item(i):
        return {"id": str(i), "uid": f"uid-{i}"}

    return {"host_identity": host, "scope": [{"source": item(i), "cuts": [item(2), item(3)]} for i in (10, 11, 12)]
            + [{"source": item(13), "cuts": [item(21), item(22)]}]}


def test_query_produces_bound_typed_source_inventory_and_geometry_preview(model, tmp_path):
    document, _, _ = model
    result = invoke(document, request(document))
    assert result["status"] == "ok", result
    snapshot = result["snapshot_json"].encode()
    inventory = json.loads(result["inventory_json"])
    assert inventory["basis_sha256"] == sha256(snapshot).hexdigest()
    assert inventory["elements"][0]["parameters"] == [
        {"identity": "parameter:-7", "storage": "element_id", "has_value": True, "value": "-8", "read_only": False},
        {"identity": "parameter:8", "storage": "string", "has_value": False, "value": None, "read_only": False},
        {"identity": "parameter:9", "storage": "double", "has_value": True,
         "value": 0.12345678901234567, "read_only": False},
        {"identity": "shared:parameter-guid", "storage": "string", "has_value": True, "value": "", "read_only": False},
    ]
    assert inventory["elements"][-1]["dependencies"] == [{"id": "20", "uid": "uid-20"}]
    assert inventory["elements"][0]["wall_end_joins"] == [
        {"end": 0, "join_allowed": True, "neighbors": []}, {"end": 1, "join_allowed": False, "neighbors": []},
    ]
    assert "fixture.rvt" not in result["snapshot_json"] + result["inventory_json"]
    evidence = tmp_path / "snapshot.json"
    evidence.write_bytes(snapshot)
    run = subprocess.run([sys.executable, str(SCRIPTS / "element_split_preview.py"), str(evidence)],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stdout or run.stderr
    report = json.loads(run.stdout)
    assert report["basis_sha256"] == inventory["basis_sha256"]
    assert [row["category"] for row in report["findings"]] == [
        "wall", "architectural_column", "structural_column", "floor",
    ]
    assert report["findings"][0]["pieces"][1] == {"base_ft": 8, "top_ft": 15}
    assert [piece["area_sqft"] for piece in report["findings"][-1]["pieces"]] == [8, 8, 16]
    assert report["steps"] == []


@pytest.mark.parametrize("change, reason", [
    ("saved_state", "saved, unchanged"),
    ("wrong_cut", "reviewed identity"),
    ("inserts", "contains inserts"),
    ("mismatched_face", "does not match"),
    ("unreadable_parameter", "storage type"),
])
def test_query_blocks_the_whole_snapshot_on_incomplete_source(model, change, reason):
    document, elements, face = model
    args = request(document)
    if change == "saved_state":
        document.IsModified = True
    elif change == "wrong_cut":
        args["scope"][0]["cuts"][0]["uid"] = "other-cut"
    elif change == "inserts":
        elements[10].FindInserts = lambda *_: [Id(77)]
    elif change == "mismatched_face":
        face.GetEdgesAsCurveLoops = lambda: [loop([(0, 0), (9, 0), (9, 4), (0, 4)], -0.5)]
    else:
        elements[10].Parameters = [parameter(1, "unsupported", 1)]
    result = invoke(document, args)
    assert result["status"] == "blocked" and reason in result["reason"]
    assert "snapshot_json" not in result and "inventory_json" not in result


@pytest.fixture
def split_readback(model, tmp_path):
    document, elements, _ = model
    args = request(document)
    args["scope"] = [args["scope"][0]]
    args["scope"][0]["cuts"] = args["scope"][0]["cuts"][:1]
    wall = elements[10]
    original_parameters = [*wall.Parameters, parameter(-1001105, "d", 20)]
    wall.Parameters = original_parameters
    source = invoke(document, args)
    assert source["status"] == "ok", source
    original_constraints = wall.get_Parameter
    upper = native(Wall, 110, **{key: value for key, value in vars(wall).items() if key not in ("Id", "UniqueId")})
    elements[110] = upper
    for piece, low, high in ((wall, 0, 8), (upper, 8, 20)):
        dimensions = {
            "WALL_BASE_OFFSET": parameter(-1001108, "d", low),
            "WALL_USER_HEIGHT_PARAM": parameter(-1001105, "d", high - low),
        }
        piece.get_Parameter = lambda key, dimensions=dimensions: dimensions.get(key) or original_constraints(key)
        piece.Parameters = [*original_parameters[:-1], dimensions["WALL_USER_HEIGHT_PARAM"]]
        piece.get_BoundingBox = lambda _, low=low, high=high: NS(
            Min=point(0, 0, low), Max=point(1, 2, high), Transform=NS(OfPoint=lambda value: value, IsIdentity=True))

    document.IsModified = True
    result = invoke(document, {"host_identity": args["host_identity"], "readback": [
        {"source_id": "10", "result": {"id": "110", "uid": "uid-110"}},
        {"source_id": "10", "result": {"id": "10", "uid": "uid-10"}},
    ]})
    assert result["status"] == "ok", result
    snapshot = tmp_path / "snapshot.json"
    inventory = tmp_path / "inventory.json"
    snapshot.write_text(source["snapshot_json"])
    inventory.write_text(source["inventory_json"])
    approval = {"kind": "element-split-readback-approval", "snapshot_sha256": sha256(snapshot.read_bytes()).hexdigest(),
                "inventory_sha256": sha256(inventory.read_bytes()).hexdigest(),
                "piece_adjustments": [
                    {"source_id": "10", "piece": 1, "values": {"parameter:-1001105": 8}},
                    {"source_id": "10", "piece": 2, "values": {"parameter:-1001105": 12}},
                ]}
    return snapshot, inventory, approval, json.loads(result["readback_json"])


@pytest.mark.parametrize("change, reason", [
    ("none", None),
    ("lost_parameter", "missing_parameter"),
    ("new_parameter", "extra_parameter"),
    ("changed_parameter", "parameter_changed"),
    ("changed_type", "type_changed"),
    ("wrong_geometry", "unmatched_or_ambiguous_geometry"),
    ("moved_footprint", "footprint_changed"),
    ("missing_piece", "missing_piece"),
    ("unapproved_parameter", "adjustments"),
    ("wrong_digest", "approval digest"),
])
def test_verifier_compares_fresh_pieces_against_exact_approved_source(tmp_path, split_readback, change, reason):
    snapshot, inventory, approval, measured = split_readback
    piece = measured["pieces"][0]
    if change == "lost_parameter":
        piece["parameters"].pop(0)
    elif change == "new_parameter":
        piece["parameters"].append({"identity": "parameter:99", "storage": "integer", "value": 2,
                                    "has_value": True, "read_only": False})
    elif change == "changed_parameter":
        next(row for row in piece["parameters"] if row["identity"] == "shared:parameter-guid")["value"] = "lost"
    elif change == "changed_type":
        piece["type_uid"] = "different-type"
    elif change == "wrong_geometry":
        piece["geometry"]["base_ft"] = 9
    elif change == "moved_footprint":
        piece["footprint_xy_ft"] = [[10, 0], [11, 2]]
    elif change == "missing_piece":
        measured["pieces"].pop()
    elif change == "unapproved_parameter":
        approval["piece_adjustments"][0]["values"]["shared:parameter-guid"] = "overridden"

    approved, readback = tmp_path / "approval.json", tmp_path / "readback.json"
    approved.write_text(json.dumps(approval))
    readback.write_text(json.dumps(measured))
    digest = "f" * 64 if change == "wrong_digest" else sha256(approved.read_bytes()).hexdigest()
    result = subprocess.run([sys.executable, str(SCRIPTS / "verify_element_split.py"),
                             str(snapshot), str(inventory), str(approved), str(readback), "--approval-sha256", digest],
                            capture_output=True, text=True)
    if change in ("unapproved_parameter", "wrong_digest"):
        assert result.returncode == 2 and result.stdout == "" and reason in result.stderr.lower()
    else:
        comparison = json.loads(result.stdout)
        if change == "none":
            assert result.returncode == 0 and comparison["status"] == "matched"
            assert comparison["matches"] == [
                {"source_id": "10", "piece": 2, "result_id": "110"},
                {"source_id": "10", "piece": 1, "result_id": "10"},
            ]
        else:
            assert result.returncode == 1 and comparison["status"] == "mismatch"
            assert reason in {row["reason"] for row in comparison["differences"]}


def test_floor_readback_checks_the_boundary_order_not_just_the_vertex_set(model):
    from verify_element_split import matching_geometry

    planned = {"elevation_ft": 0, "boundary_xy_ft": [(0, 0), (4, 0), (4, 2), (0, 2)]}
    reversed_ring = {"elevation_ft": 0, "boundary_xy_ft": [[4, 2], [4, 0], [0, 0], [0, 2]]}
    crossed = {"elevation_ft": 0, "boundary_xy_ft": [[4, 2], [0, 0], [4, 0], [0, 2]]}
    assert matching_geometry("floor", planned, reversed_ring)
    assert not matching_geometry("floor", planned, crossed)


class Changed:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, document, name, operation, added=(), modified=(), deleted=()):
        event = NS(GetDocument=lambda: document, Operation=operation, GetTransactionNames=lambda: [name],
                   GetAddedElementIds=lambda: list(map(Id, added)), GetModifiedElementIds=lambda: list(map(Id, modified)),
                   GetDeletedElementIds=lambda: list(map(Id, deleted)))
        for handler in self.handlers:
            handler(None, event)


def install_trial_transactions(db, document, elements, created, changes, status, fault, original):
    started = []
    class Transaction:
        def __init__(self, _document, name):
            self.name = name
            self.state = status.Uninitialized

        def Start(self):
            started.append(type(self).__name__)
            self.state = status.Started
            return self.state

        def GetStatus(self):
            return self.state

        def Commit(self):
            self.state = status.Committed
            changes.emit(document, self.name, "TransactionCommitted", added=created,
                         modified=[original.Id, 100] if fault == "extra_edit" else [original.Id])
            return self.state

        def RollBack(self):
            self.state = status.RolledBack
            return self.state

    class Group(Transaction):
        def RollBack(self):
            self.state = status.RolledBack
            elements[original.Id] = original
            if fault != "residual":
                for identifier in created:
                    del elements[identifier]
            document.IsModified = False
            if fault != "no_rollback_event":
                changes.emit(document, self.name, "TransactionGroupRolledBack", modified=[original.Id], deleted=created)
            return self.state

    db.Transaction = Transaction
    db.TransactionGroup = Group
    return started


@pytest.mark.parametrize("fault, expected", [
    ("none", "trial_rolled_back"),
    ("digest", "stopped"),
    ("extra_edit", "transaction_unresolved"),
    ("no_rollback_event", "transaction_unresolved"),
    ("residual", "transaction_unresolved"),
])
def test_column_trial_checks_normalization_effects_and_terminal_rollback(model, monkeypatch, fault, expected):
    document, elements, _ = model
    db = sys.modules["pyrevit.api"].DB
    monkeypatch.delitem(sys.modules, "read_element_split_evidence", raising=False)
    ratios, created = [], []
    changes = Changed()
    document.Application.DocumentChanged = changes
    document.Equals = lambda other: document is other
    document.Regenerate = lambda: None
    db.XYZ = point
    status = db.TransactionStatus = NS(Uninitialized="new", Started="started", Committed="committed",
                                       RolledBack="rolled_back")
    original_parameters = elements[12].Parameters

    def column(identifier, low, high):
        original = elements[12]
        common = {key: value for key, value in vars(original).items()
                  if key not in ("Id", "UniqueId", "get_Parameter", "get_BoundingBox", "Location", "Split", "Parameters")}
        segment = Line((0, 0, high), (0, 0, low))
        segment.Project = lambda target: NS(Parameter=target.Z, Distance=0)
        segment.ComputeNormalizedParameter = lambda z: (high - z) / (high - low)
        parameters = {
            "FAMILY_BASE_LEVEL_PARAM": parameter(-1001350, "e", Id(1)),
            "FAMILY_TOP_LEVEL_PARAM": parameter(-1001351, "e", Id(4)),
            "FAMILY_BASE_LEVEL_OFFSET_PARAM": parameter(-1001357, "d", low),
            "FAMILY_TOP_LEVEL_OFFSET_PARAM": parameter(-1001358, "d", high - 20),
        }

        def split(normalized):
            ratios.append(normalized)
            cut = high - (high - low) * normalized
            new = 200 + len(created)
            created.append(new)
            elements[identifier] = column(identifier, low, cut)
            elements[new] = column(new, cut, high)
            document.IsModified = True
            return Id(new)

        return native(FamilyInstance, identifier, **common, Location=LocationCurve(Curve=segment),
                      Parameters=[*original_parameters, parameters["FAMILY_BASE_LEVEL_OFFSET_PARAM"],
                                  parameters["FAMILY_TOP_LEVEL_OFFSET_PARAM"]], get_Parameter=parameters.get,
                      get_BoundingBox=lambda _: NS(Min=point(0, 0, low), Max=point(1, 2, high),
                                                 Transform=NS(OfPoint=lambda value: value, IsIdentity=True)),
                      Split=split)

    elements[12] = original = column(12, 0, 20)

    started = install_trial_transactions(db, document, elements, created, changes, status, fault, original)
    before_request = request(document)
    before_request["scope"] = [before_request["scope"][2]]
    before = invoke(document, before_request)
    assert before["status"] == "ok", before
    approval = json.dumps({"kind": "element-split-readback-approval",
                           "snapshot_sha256": sha256(before["snapshot_json"].encode()).hexdigest(),
                           "inventory_sha256": sha256(before["inventory_json"].encode()).hexdigest(),
                           "piece_adjustments": [
                               {"source_id": "12", "piece": index, "values": {
                                   "parameter:-1001357": low, "parameter:-1001358": high - 20}}
                               for index, (low, high) in enumerate(((0, 8), (8, 15), (15, 20)), 1)]})
    approval_sha = sha256(approval.encode()).hexdigest()
    result = runpy.run_path(str(SCRIPTS / "probe_element_split.py"), init_globals={"doc": document, "inputs": {
        "mode": "rollback_trial", "disposable_fixture": True, "source_request": before_request,
        "approval_json": approval, "approval_sha256": "f" * 64 if fault == "digest" else approval_sha,
    }})["result"]
    assert result["status"] == expected, result
    assert not changes.handlers
    if fault == "digest":
        assert started == []
        return
    assert ratios == pytest.approx([0.6, 5 / 12])
    assert started == ["Group", "Transaction"]
    assert (200 not in elements and 201 not in elements) == (fault != "residual")
    if expected == "trial_rolled_back":
        receipt = json.loads(result["trial_json"])
        assert receipt["inner_effects"] == {"added": ["200", "201"], "modified": ["12"], "deleted": []}
        assert receipt["rollback_events"][0]["deleted"] == ["200", "201"]
        assert receipt["comparison"]["status"] == "matched"
        assert invoke(document, before_request) == before


@pytest.mark.parametrize("fault, expected", [
    ("none", "trial_rolled_back"),
    ("offset_location", "stopped"),
    ("joined_wall", "stopped_and_rolled_back"),
    ("cannot_copy", "stopped_and_rolled_back"),
    ("top_constrained", "trial_rolled_back"),
    ("bad_faces", "stopped_and_rolled_back"),
    ("bad_volume", "stopped_and_rolled_back"),
])
def test_wall_trial_preserves_level_orientation_type_and_parameters(model, monkeypatch, fault, expected):
    document, elements, _ = model
    db = sys.modules["pyrevit.api"].DB
    monkeypatch.delitem(sys.modules, "read_element_split_evidence", raising=False)
    status = db.TransactionStatus = NS(Uninitialized="new", Started="started", Committed="committed",
                                       RolledBack="rolled_back")
    db.WallLocationLine = NS(WallCenterline=0)
    db.WallUtils.IsWallJoinAllowedAtEnd = lambda wall, end: wall.join_allowed[end]
    db.WallUtils.DisallowWallJoinAtEnd = lambda wall, end: wall.join_allowed.__setitem__(end, False)
    db.XYZ = point
    db.ShellLayerType = NS(Exterior="exterior", Interior="interior")
    db.HostObjectUtils.GetSideFaces = lambda wall, layer: [layer]
    db.Options = lambda: NS(Dispose=lambda: None)
    db.Solid = type("Solid", (NS,), {})
    db.BuiltInParameter.WALL_KEY_REF_PARAM = "WALL_KEY_REF_PARAM"
    db.BuiltInParameter.WALL_STRUCTURAL_SIGNIFICANT = "WALL_STRUCTURAL_SIGNIFICANT"
    monkeypatch.setattr(Line, "CreateBound", staticmethod(
        lambda a, b: Line((a.X, a.Y, a.Z), (b.X, b.Y, b.Z))), raising=False)
    changes = Changed()
    document.Application.DocumentChanged = changes
    document.Equals = lambda other: document is other
    document.Regenerate = lambda: None
    created, constructor_calls = [], []
    template = elements[10]

    def mutable(identifier, storage, initial, reject=False, **details):
        item = parameter(identifier, storage, initial, **details)
        item.current = initial
        item.AsDouble = item.AsString = item.AsElementId = lambda: item.current

        def set_value(value):
            if reject:
                return False
            item.current = value
            document.IsModified = True
            return True

        item.Set = set_value
        return item

    def wall(identifier, native_line, base_level, height, finish, flipped=True, structural=True):
        has_top = fault == "top_constrained" and identifier == 10
        top_level = mutable(-1001103, "e", Id(4) if has_top else Id(-1))
        top_offset = mutable(-1001109, "d", -1 if has_top else 0)
        height_parameter = mutable(-1001105, "d", height)
        base_parameter = parameter(-1001107, "e", base_level)
        finish_parameter = mutable(50, "s", finish, reject=fault == "cannot_copy" and identifier != 10,
                                   GUID="wall-finish")
        values = {
            "WALL_BASE_CONSTRAINT": base_parameter,
            "WALL_BASE_OFFSET": parameter(-1001108, "d", 0),
            "WALL_HEIGHT_TYPE": top_level,
            "WALL_TOP_OFFSET": top_offset,
            "WALL_USER_HEIGHT_PARAM": height_parameter,
            "WALL_TOP_IS_ATTACHED": parameter(-14, "i", 0),
            "WALL_BOTTOM_IS_ATTACHED": parameter(-15, "i", 0),
            "WALL_KEY_REF_PARAM": parameter(-1001122, "i", int(fault == "offset_location")),
            "WALL_STRUCTURAL_SIGNIFICANT": parameter(-1001596, "i", int(structural)),
        }
        base = elements[base_level].ProjectElevation
        def top():
            return (base + height_parameter.current if top_level.current == Id(-1)
                    else elements[top_level.current].ProjectElevation + top_offset.current)

        def side(layer):
            exterior = layer == "exterior"
            y = 0.5 if exterior else 0
            span = top()
            corners = [(0, y, base), (4, y, base), (4, y, span), (0, y, span)]
            if fault == "bad_faces" and identifier != 10:
                corners = [corners[i] for i in (0, 2, 1, 3)]
            return PlanarFace(FaceNormal=point(0, 1 if exterior else -1, 0),
                              GetEdgesAsCurveLoops=lambda: [[Line(a, b) for a, b in
                                  zip(corners, corners[1:] + corners[:1])]])

        location = LocationCurve(Curve=native_line, neighbors=[[elements[100]] if fault == "joined_wall"
                                                                and identifier != 10 else [], []])
        common = {key: value for key, value in vars(template).items()
                  if key not in ("Id", "UniqueId", "get_Parameter", "get_BoundingBox", "Location",
                                 "Parameters", "Orientation", "Flipped")}
        return native(Wall, identifier, **common, Location=location, join_allowed=[True, identifier != 10],
                      Orientation=point(0, 1 if flipped else -1, 0), Flipped=flipped,
                      Parameters=[*values.values(), finish_parameter], get_Parameter=values.get,
                      GetGeometryObjectFromReference=side,
                      get_Geometry=lambda _: [db.Solid(Volume=2 * (top() - base) + int(
                          fault == "bad_volume" and identifier != 10))],
                      get_BoundingBox=lambda _: NS(
                          Min=point(0, 0, base), Max=point(4, 0.5, top()),
                          Transform=NS(OfPoint=lambda value: value, IsIdentity=True)))

    def create(_, line, type_id, level_id, height, offset, flipped, structural):
        identifier = 300 + len(created)
        created.append(identifier)
        constructor_calls.append((type_id.Value, level_id.Value, height, offset, flipped, structural,
                                  line.GetEndPoint(0).Z))
        elements[identifier] = wall(identifier, line, level_id, height, "Default", flipped, structural)
        document.IsModified = True
        return elements[identifier]

    monkeypatch.setattr(Wall, "Create", staticmethod(create), raising=False)
    original_line = Line((0, 0, 0), (4, 0, 0))
    elements[10] = wall(10, original_line, Id(1), 20, "Reviewed")
    original = wall(10, original_line, Id(1), 20, "Reviewed")
    started = install_trial_transactions(db, document, elements, created, changes, status, fault, original)
    before_request = request(document)
    before_request["scope"] = [before_request["scope"][0]]
    before = invoke(document, before_request)
    assert before["status"] == "ok", before
    if fault == "top_constrained":
        adjustments = [(1, 20, 2, 0), (2, 7, 3, 0), (3, 4, 4, -1)]
    else:
        adjustments = [(1, 8, -1, 0), (2, 7, -1, 0), (3, 5, -1, 0)]
    approval = json.dumps({"kind": "element-split-readback-approval",
                           "snapshot_sha256": sha256(before["snapshot_json"].encode()).hexdigest(),
                           "inventory_sha256": sha256(before["inventory_json"].encode()).hexdigest(),
                           "piece_adjustments": [
                               {"source_id": "10", "piece": index, "values": {
                                   "parameter:-1001107": str(base_id), "parameter:-1001105": height,
                                   "parameter:-1001103": str(top_id), "parameter:-1001109": offset}}
                               for index, (base_id, height, top_id, offset) in enumerate(adjustments, 1)]})
    result = runpy.run_path(str(SCRIPTS / "probe_element_split.py"), init_globals={"doc": document, "inputs": {
        "mode": "rollback_trial", "disposable_fixture": True, "source_request": before_request,
        "approval_json": approval, "approval_sha256": sha256(approval.encode()).hexdigest(),
    }})["result"]
    assert result["status"] == expected, result
    assert invoke(document, before_request) == before
    assert not any(identifier in elements for identifier in created)
    if fault == "offset_location":
        assert started == []
    elif fault in ("none", "top_constrained"):
        final_height = 4 if fault == "top_constrained" else 5
        assert constructor_calls == [(100, 2, 7, 0, True, True, 8), (100, 3, final_height, 0, True, True, 15)]
        receipt = json.loads(result["trial_json"])
        assert receipt["inner_effects"] == {"added": ["300", "301"], "modified": ["10"], "deleted": []}
        measured = json.loads(receipt["readback_json"])
        assert all(any(row["identity"] == "shared:wall-finish" and row["value"] == "Reviewed"
                       for row in item["parameters"]) for item in measured["pieces"])
        assert [receipt["wall_measurements"][key]["solid_volume_cuft"] for key in ("10", "300", "301")] == [
            16, 14, 2 * final_height,
        ]


@pytest.mark.parametrize("fault, expected", [
    ("none", "floor_cascade_rolled_back"),
    ("wrong_digest", "stopped"),
    ("external_dependency", "stopped"),
    ("extra_delete", "transaction_unresolved"),
    ("partial_ids", "transaction_unresolved"),
    ("failed_restore", "transaction_unresolved"),
])
def test_floor_cascade_discovery_reconciles_exact_owned_ids_after_rollback(model, monkeypatch, fault, expected):
    document, elements, _ = model
    db = sys.modules["pyrevit.api"].DB
    monkeypatch.delitem(sys.modules, "read_element_split_evidence", raising=False)
    status = db.TransactionStatus = NS(Uninitialized="new", Started="started", RolledBack="rolled_back")
    owned = native(ModelLine, 23, GeometryCurve=Line((0, 0, 0), (8, 0, 0)))
    elements[23] = owned
    floor, sketch = elements[13], elements[20]
    floor.GetDependentElements = lambda _: [Id(20), Id(23)] + ([Id(100)] if fault == "external_dependency" else [])
    sketch.GetAllElements = lambda: [Id(23)]
    sketch.GetDependentElements = lambda _: [Id(23)]
    baseline = dict(elements)
    starts, rolled = [], []

    class Transaction:
        def __init__(self, _document, name):
            self.name = name
            self.state = status.Uninitialized

        def Start(self):
            starts.append(self.name)
            self.state = status.Started
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            elements.update(baseline)
            if fault == "failed_restore":
                elements[23] = native(ModelLine, 999)
            document.IsModified = document.IsModifiable = False
            self.state = status.RolledBack
            rolled.append(self.name)
            return self.state

    def delete(identifier):
        assert identifier == Id(13)
        ids = [Id(13), Id(20), Id(23)]
        if fault == "extra_delete":
            ids.append(Id(100))
        for value in ids:
            del elements[value]
        document.IsModified = document.IsModifiable = True
        yield ids[0]
        if fault == "partial_ids":
            raise RuntimeError("native ID iterator failed")
        yield from ids[1:]

    db.Transaction = Transaction
    document.Delete = delete
    before_request = request(document)
    before_request["scope"] = [before_request["scope"][-1]]
    before = invoke(document, before_request)
    assert before["status"] == "ok", before
    snapshot_sha = sha256(before["snapshot_json"].encode()).hexdigest()
    inventory_sha = sha256(before["inventory_json"].encode()).hexdigest()
    response = runpy.run_path(str(SCRIPTS / "probe_floor_split_cascade.py"), init_globals={
        "doc": document, "inputs": {"mode": "floor_cascade_trial", "disposable_fixture": True,
                                    "source_request": before_request,
                                    "snapshot_sha256": "0" * 64 if fault == "wrong_digest" else snapshot_sha,
                                    "inventory_sha256": inventory_sha},
    })["result"]
    assert response["status"] == expected, response
    assert starts == rolled
    if fault in ("wrong_digest", "external_dependency"):
        assert starts == []
    else:
        assert len(rolled) == 1 and not document.IsModified
    if fault == "none":
        receipt = json.loads(response["cascade_json"])
        assert receipt["deleted"] == [{"id": "13", "uid": "uid-13"}, {"id": "20", "uid": "uid-20"},
                                      {"id": "23", "uid": "uid-23"}]
        assert receipt["source"] == {"id": "13", "uid": "uid-13"}
        assert receipt["sketch"] == {"id": "20", "uid": "uid-20"}
        assert receipt["snapshot_sha256"] == snapshot_sha and receipt["inventory_sha256"] == inventory_sha
        assert invoke(document, before_request) == before
    else:
        assert "cascade_json" not in response
