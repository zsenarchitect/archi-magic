import importlib
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


class ElementId:
    def __init__(self, value):
        self.Value = value


class Line:
    def __init__(self, start, end):
        self.ends = start, end

    CreateBound = classmethod(lambda cls, start, end: cls(start, end))

    def GetType(self):
        return NS(Name="Line")

    def GetEndPoint(self, index):
        return self.ends[index]


class Entity:
    def __init__(self, schema):
        self.value = None
        self.Get = {str: lambda _field: self.value}
        self.Set = {str: self.set_value}

    def set_value(self, _field, value):
        self.value = value

    def IsValid(self):
        return self.value is not None


@pytest.mark.parametrize("outcome", [
    "committed", "offset_mismatch", "provenance_missing", "unqualified_schema", "stale_profile",
    "modified_existing", "unrelated_add", "missing_root", "commit_changed",
    "workflow_commit", "workflow_stale", "workflow_existing", "workflow_owned", "workflow_mapping_changed",
])
def test_room_finish_batch_checks_native_geometry_and_rolls_back_as_one_unit(monkeypatch, outcome):
    def xyz(x, y, z):
        return NS(X=x, Y=y, Z=z)

    level = type("Level", (), {"UniqueId": "level", "ProjectElevation": 25.0})
    floor_type = type("FloorType", (), {"UniqueId": "floor-type", "IsFoundationSlab": False})
    ceiling_type = type("CeilingType", (), {"UniqueId": "ceiling-type"})
    wall_type = type("WallType", (), {"UniqueId": "wall-type", "Kind": "basic"})
    sketch_class = type("Sketch", (), {})
    model_curve = type("ModelCurve", (), {})
    schema = NS(SchemaName="ArchiMagicRoomFinishV1", ListFields=lambda: [
        NS(FieldName="Payload", ValueType=NS(FullName="System.String"), ContainerType="simple"),
    ])
    ext = NS(Entity=Entity, ContainerType=NS(Simple="simple"), ExtensibleStorageFilter=str,
             Schema=NS(Lookup=lambda _guid: None if outcome == "unqualified_schema" else schema))
    status = NS(Started="started", Committed="committed", RolledBack="rolled-back", Pending="pending")
    events = []
    group_events = []
    created = []

    class ChangeEvent:
        def __init__(self):
            self.handlers = []

        def __iadd__(self, handler):
            self.handlers.append(handler)
            return self

        def __isub__(self, handler):
            self.handlers.remove(handler)
            return self

    class Transaction:
        def __init__(self, document, name):
            self.document = document
            self.name = name

        def Start(self):
            events.append(status.Started)
            return status.Started

        def Commit(self):
            events.append(status.Committed)
            original_ids = {10, 20, 30, 40, 50, 1}
            added = set(self.document.elements) - original_ids
            if outcome == "unrelated_add":
                added.add(999)
            if outcome == "missing_root":
                added.remove(created[0].Id.Value)
            if outcome == "commit_changed":
                created[0].values["floor-offset"] += 1
            event = NS(GetDocument=lambda: self.document, GetTransactionNames=lambda: [self.name],
                       GetAddedElementIds=lambda: [ElementId(value) for value in added],
                       GetModifiedElementIds=lambda: [ElementId(50)] if outcome == "modified_existing" else [],
                       GetDeletedElementIds=lambda: [])
            for handler in self.document.Application.DocumentChanged.handlers:
                handler(None, event)
            return status.Committed

        def GetStatus(self):
            return events[-1]

        def RollBack(self):
            events.append(status.RolledBack)
            return status.RolledBack

    class Group:
        def __init__(self, document, _name):
            self.document = document
            self.original = dict(document.elements)

        def Start(self):
            group_events.append(status.Started)
            return status.Started

        def GetStatus(self):
            return group_events[-1]

        def RollBack(self):
            group_events.append(status.RolledBack)
            self.document.elements = self.original
            created[:] = []
            return status.RolledBack

        def Assimilate(self):
            group_events.append(status.Committed)
            return status.Committed

    class Finish:
        DesignOption = None
        CreatedPhaseId, DemolishedPhaseId = ElementId(20), ElementId(-1)
        is_wall = False

        @classmethod
        def Create(cls, document, geometry, type_id, level_id, *wall_args):
            item = cls()
            item.Id = ElementId(200 + len(created))
            item.UniqueId = "finish-" + str(item.Id.Value)
            item.LevelId = level_id
            item.GetTypeId = lambda: type_id
            item.record = Entity(schema)
            item.values = {}
            if cls.is_wall:
                height, offset, flip, structural = wall_args
                assert (height, offset, flip, structural) == (9.0, 0.25, False, False)
                item.Location = NS(Curve=geometry)
                item.joins = [True, True]
                item.values.update(height=height, base=offset, top=ElementId(-1), bounding=1)
            else:
                assert not wall_args
                item.SketchId = ElementId(item.Id.Value + 10)
                sketch = sketch_class()
                sketch.Id, sketch.OwnerId, sketch.Profile = item.SketchId, item.Id, geometry
                sketch.UniqueId = "sketch-" + str(sketch.Id.Value)
                sketch.SketchPlane = None
                curve_id = ElementId(item.Id.Value + 100)
                sketch.GetAllElements = lambda: [curve_id]
                document.elements[item.SketchId.Value] = sketch
                curve = model_curve()
                curve.Id, curve.UniqueId = curve_id, "curve-" + str(curve_id.Value)
                document.elements[curve_id.Value] = curve
            document.elements[item.Id.Value] = item
            created.append(item)
            return item

        def get_Parameter(self, key):
            def assign(value):
                self.values[key] = value + 0.125 if outcome == "offset_mismatch" and key == "ceiling-offset" else value
                return True

            return NS(IsReadOnly=False, HasValue=True, Set=assign,
                      StorageType={"bounding": "integer", "top": "id"}.get(key, "double"),
                      AsDouble=lambda: self.values[key], AsInteger=lambda: self.values[key],
                      AsElementId=lambda: self.values[key])

        def SetEntity(self, entity):
            if outcome != "provenance_missing" or not self.is_wall:
                self.record = entity

        def GetEntity(self, _schema):
            return self.record

    wall = type("Wall", (Finish,), {"is_wall": True})
    floor = type("Floor", (Finish,), {})
    ceiling = type("Ceiling", (Finish,), {})
    names = NS(FLOOR_HEIGHTABOVELEVEL_PARAM="floor-offset", CEILING_HEIGHTABOVELEVEL_PARAM="ceiling-offset",
               WALL_BASE_OFFSET="base", WALL_USER_HEIGHT_PARAM="height", WALL_HEIGHT_TYPE="top",
               WALL_ATTR_ROOM_BOUNDING="bounding")
    db = NS(ElementId=ElementId, Level=level, FloorType=floor_type, CeilingType=ceiling_type, WallType=wall_type,
            Floor=floor, Ceiling=ceiling, Wall=wall, Sketch=sketch_class, Line=Line, XYZ=xyz, WallKind=NS(Basic="basic"),
            Transaction=Transaction, TransactionGroup=Group, TransactionStatus=status, BuiltInParameter=names,
            ModelCurve=model_curve, ReferencePlane=type("ReferencePlane", (), {}), Dimension=type("Dimension", (), {}),
            BoundaryValidation=NS(IsValidHorizontalBoundary=lambda profiles: len(profiles) == 1),
            StorageType=NS(Integer="integer", Double="double", ElementId="id"),
            WallUtils=NS(DisallowWallJoinAtEnd=lambda item, end: item.joins.__setitem__(end, False),
                         IsWallJoinAllowedAtEnd=lambda item, end: item.joins[end]))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.DB.ExtensibleStorage", ext)
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int, Guid=str, String=str))
    for name in ("room_finish_provenance", "room_finish_placement"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    placement = importlib.import_module("room_finish_placement")
    provenance = importlib.import_module("room_finish_provenance")
    document = NS(IsFamilyDocument=False, IsReadOnly=False, IsModifiable=False,
                  Application=NS(ShortCurveTolerance=0.01, DocumentChanged=ChangeEvent()), Regenerate=lambda: None,
                  elements={10: level(), 20: NS(UniqueId="phase"), 30: floor_type(), 40: ceiling_type(), 50: wall_type()})
    document.GetElement = lambda element_id: (
        next((item for item in document.elements.values() if getattr(item, "UniqueId", None) == element_id), None)
        if isinstance(element_id, str) else document.elements.get(element_id.Value)
    )
    document.Equals = lambda other: other is document
    points = [xyz(0, 0, 25), xyz(4, 0, 25), xyz(4, 4, 25), xyz(0, 4, 25)]
    profile = [Line(a, b) for a, b in zip(points, points[1:] + points[:1])]
    profile_key = [sorted(provenance.native_edge(curve) for curve in profile)]
    axis = [[0, 0.125, 25], [4, 0.125, 25]]
    common = {"level_uid": "level", "phase_uid": "phase"}
    intentions = [
        dict(common, kind="floor", type_uid="floor-type", profile=profile_key, offset_ft=0.125),
        dict(common, kind="ceiling", type_uid="ceiling-type", profile=profile_key, offset_ft=9.0),
        dict(common, kind="wall", type_uid="wall-type", centerline=provenance.edge_key("Line", *axis),
             base_offset_ft=0.25, height_ft=9.0),
    ]
    pieces = [{"room_uid": "room", "piece_key": provenance.digest(intended), "intended": intended,
               "level_id": "10", "type_id": str(type_id), "profiles": [profile], "centerline_ft": axis}
              for type_id, intended in zip((30, 40, 50), intentions)]
    if outcome.startswith("workflow_"):
        class CurveLoop(list):
            Append = list.append

            def Dispose(self):
                pass

        class GenericList:
            def __getitem__(self, _):
                return type("Profiles", (list,), {"Add": list.append})

        room_type = type("Room", (), {"UniqueId": "room"})
        room = room_type()
        room.GetBoundarySegments = lambda _options: [[NS(GetCurve=lambda c=curve: c) for curve in profile]]
        db.Architecture = NS(Room=room_type)
        db.CurveLoop = CurveLoop
        db.SpatialElementBoundaryOptions = lambda: NS(Dispose=lambda: None)
        db.SpatialElementBoundaryLocation = NS(Finish="finish")
        db.BuiltInParameter.VIEW_PHASE = "view-phase"
        db.DesignOption = NS(GetActiveDesignOptionId=lambda _doc: ElementId(-1))
        db.WorksharingUtils = NS(
            GetCheckoutStatus=lambda _doc, _id: "OwnedByOtherUser",
            GetModelUpdatesStatus=lambda _doc, _id: "CurrentWithCentral",
        )
        document.Application.VersionNumber = "2026"
        document.IsWorkshared = outcome == "workflow_owned"
        document.ActiveView = NS(IsTemplate=False, UniqueId="fixture-view",
                                 get_Parameter=lambda _name: NS(StorageType="id", AsElementId=lambda: ElementId(20)))
        document.elements[1] = room
        audit = {"nearby": {"status": "none_in_scope"},
                 "status": "tagged_set_matches" if outcome == "workflow_existing" else "no_tagged_pieces"}
        edges = [{"curve_type": "Line", "source_id": 70, "linked_source_id": -1,
                  "start_ft": provenance.native_xyz(curve.GetEndPoint(0)),
                  "end_ft": provenance.native_xyz(curve.GetEndPoint(1))} for curve in profile]
        row = {"status": "boundary_verified", "room_id": 1, "room_uid": "room", "boundary_loops": [edges],
               "level": {"id": "10", "uid": "level"}, "phase": {"id": "20", "uid": "phase"},
               "horizontal_finishes": [{"kind": "floor", "type_id": "30", "type_uid": "floor-type",
                                        "offset_ft": 0.125}],
               "existing_finishes": audit}
        report = {"rooms": [row], "host_identity": {"project_uid": "project", "path_sha256": "a" * 64}}
        monkeypatch.setitem(sys.modules, "System.Collections.Generic", NS(List=GenericList()))
        monkeypatch.setitem(sys.modules, "read_room_finish_boundaries", NS(
            ids_from=lambda values: list(map(int, values)),
            read_boundaries=lambda *_args: report,
        ))
        script = placement.__file__

        def execute(**inputs):
            return runpy.run_path(script, init_globals={"doc": document, "inputs": inputs})["result"]

        request = {"room_ids": ["1"], "horizontal_finishes": {"floor": {"type_id": "30", "offset_ft": 0.125}}}
        if outcome == "workflow_mapping_changed":
            choice = {"horizontal_finishes": request.pop("horizontal_finishes")}
            request["finish_rules"] = {"parameter": "Finish Code", "by_value": {"A": choice, "B": choice}}
            row["finish_rule"] = {"parameter": "Finish Code", "parameter_id": "305", "value": "A"}
        if outcome == "workflow_commit":
            configured = []

            def register():
                assert configured == [
                    ("name", "ArchiMagicRoomFinishV1"), ("read", "public"), ("write", "public"),
                    ("field", "Payload", str),
                ]
                ext.Schema.Lookup = lambda _guid: schema
                return schema

            ext.Schema.Lookup = lambda _guid: None
            ext.AccessLevel = NS(Public="public")
            ext.SchemaBuilder = lambda _guid: NS(
                SetSchemaName=lambda value: configured.append(("name", value)),
                SetReadAccessLevel=lambda value: configured.append(("read", value)),
                SetWriteAccessLevel=lambda value: configured.append(("write", value)),
                AddSimpleField=lambda name, value: configured.append(("field", name, value)),
                Finish=register, Dispose=lambda: None,
            )
            monkeypatch.setitem(sys.modules, "clr", NS(GetClrType=lambda value: value))
        assert execute(mode="schema-check")["status"] == "schema_checked"
        assert not created and not events
        preview = json.loads(execute(mode="preview", request=request)["preview_json"])
        if outcome == "workflow_existing":
            assert not preview["steps"] and execute(mode="apply", plan=preview) == {"status": "no_changes"}
            assert not created and not events
            return
        assert len(preview["steps"]) == 1 and preview["ownership_ids"] == ["1", "70"]
        if outcome == "workflow_stale":
            document.ActiveView.UniqueId = "changed-view"
        if outcome == "workflow_mapping_changed":
            assert preview["columns"][-1] == "Room finish code" and preview["after"][0][-1] == "A"
            row["finish_rule"]["value"] = "B"
        receipt = execute(mode="apply", plan=preview)
        if outcome == "workflow_commit":
            assert events == ["started", "committed"] and receipt["status"] == "ok"
            assert group_events == ["started", "committed"] and receipt["added_sketch_element_ids"] == ["210", "300"]
            assert receipt["created"][0]["piece_key"] == preview["steps"][0]["piece_key"]
            assert receipt["basis_sha256"] == preview["basis_sha256"]
            assert receipt["added_sketch_elements"] == [
                {"id": "210", "uid": "sketch-210"}, {"id": "300", "uid": "curve-300"},
            ]
            keep = {"mode": "verify-keep", "plan": preview, "receipt": receipt}
            assert "persisted room finish set" in execute(**keep)["message"]
            audit["status"] = "tagged_set_matches"
            assert execute(**keep) == {
                "status": "keep_verified", "basis_sha256": preview["basis_sha256"],
                "created": receipt["created"], "added_sketch_elements": receipt["added_sketch_elements"],
                "repeat_creates": 0,
            }
            repeat = json.loads(execute(mode="preview", request=request)["preview_json"])
            assert execute(mode="apply", plan=repeat) == {"status": "no_changes"}
            created[0].values["floor-offset"] = 3.5
            assert "does not match the reviewed geometry" in execute(**keep)["message"]
            created[0].values["floor-offset"] = 0.125
            document.elements[300].UniqueId = "replacement-curve"
            assert "does not resolve by both ID and UID" in execute(**keep)["message"]
            document.elements[300].UniqueId = "curve-300"
            row["level"]["id"] = "different"
            assert "original room scope changed" in execute(**keep)["message"]
            row["level"]["id"] = "10"
            audit["status"] = "no_tagged_pieces"
            rollback = {"mode": "verify-rollback", "plan": preview, "receipt": receipt}
            assert "still resolves" in execute(**rollback)["message"]
            for identifier in (200, 210, 300):
                document.elements.pop(identifier)
            assert execute(**rollback) == {
                "status": "rollback_verified", "basis_sha256": preview["basis_sha256"],
                "confirmed_absent": [
                    {"id": "200", "uid": "finish-200"},
                    {"id": "210", "uid": "sketch-210"},
                    {"id": "300", "uid": "curve-300"},
                ],
            }
            document.elements[999] = NS(UniqueId="curve-300")
            assert "still resolves by ID or UID" in execute(**rollback)["message"]
            document.elements.pop(999)
            report["host_identity"] = {"project_uid": "changed", "path_sha256": "a" * 64}
            assert "scope changed after rollback" in execute(**rollback)["message"]
            assert events == ["started", "committed"]
        else:
            assert receipt["status"] == "stopped" and not events and not created
        return
    if outcome == "stale_profile":
        pieces[0]["profiles"] = [[]]
    if outcome in ("unqualified_schema", "stale_profile"):
        with pytest.raises(ValueError, match="schema|profile"):
            placement.place_room_finishes(document, pieces)
        assert events == [] and created == []
        return
    receipt = placement.place_room_finishes(document, pieces)
    assert not document.Application.DocumentChanged.handlers
    if outcome == "committed":
        assert events == ["started", "committed"] and receipt["status"] == "ok"
        assert group_events == ["started", "committed"]
        assert receipt["added_sketch_element_ids"] == ["210", "211", "300", "301"]
        assert [item["piece_key"] for item in receipt["created"]] == [piece["piece_key"] for piece in pieces]
        assert [json.loads(item.record.value)["kind"] for item in created] == ["floor", "ceiling", "wall"]
        assert created[-1].values["bounding"] == 0 and created[-1].joins == [False, False]
    else:
        post_commit = outcome in ("modified_existing", "unrelated_add", "missing_root", "commit_changed")
        assert receipt["target_rollback_verified"] is True and not created
        assert events == ["started", "committed" if post_commit else "rolled-back"]
        assert group_events == ["started", "rolled-back"]
        assert sorted(document.elements) == [10, 20, 30, 40, 50]
