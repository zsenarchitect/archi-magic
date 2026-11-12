import importlib
import json
from pathlib import Path
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


@pytest.mark.parametrize("outcome", ["committed", "offset_mismatch", "provenance_missing", "unqualified_schema", "stale_profile"])
def test_room_finish_batch_checks_native_geometry_and_rolls_back_as_one_unit(monkeypatch, outcome):
    def xyz(x, y, z):
        return NS(X=x, Y=y, Z=z)

    level = type("Level", (), {"UniqueId": "level", "ProjectElevation": 25.0})
    floor_type = type("FloorType", (), {"UniqueId": "floor-type", "IsFoundationSlab": False})
    ceiling_type = type("CeilingType", (), {"UniqueId": "ceiling-type"})
    wall_type = type("WallType", (), {"UniqueId": "wall-type", "Kind": "basic"})
    sketch_class = type("Sketch", (), {})
    schema = NS(SchemaName="ArchiMagicRoomFinishV1", ListFields=lambda: [
        NS(FieldName="Payload", ValueType=NS(FullName="System.String"), ContainerType="simple"),
    ])
    ext = NS(Entity=Entity, ContainerType=NS(Simple="simple"), ExtensibleStorageFilter=str,
             Schema=NS(Lookup=lambda _guid: None if outcome == "unqualified_schema" else schema))
    status = NS(Started="started", Committed="committed", RolledBack="rolled-back")
    events = []
    created = []

    class Transaction:
        def __init__(self, document, _name):
            self.document = document

        def Start(self):
            events.append(status.Started)
            return status.Started

        def Commit(self):
            events.append(status.Committed)
            return status.Committed

        def GetStatus(self):
            return events[-1]

        def RollBack(self):
            events.append(status.RolledBack)
            for item in created:
                if hasattr(item, "SketchId"):
                    del self.document.elements[item.SketchId.Value]
            created[:] = []
            return status.RolledBack

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
                sketch.OwnerId, sketch.Profile = item.Id, geometry
                document.elements[item.SketchId.Value] = sketch
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
            Transaction=Transaction, TransactionStatus=status, BuiltInParameter=names,
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
                  Application=NS(ShortCurveTolerance=0.01), Regenerate=lambda: None,
                  elements={10: level(), 20: NS(UniqueId="phase"), 30: floor_type(), 40: ceiling_type(), 50: wall_type()})
    document.GetElement = lambda element_id: document.elements.get(element_id.Value)
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
    if outcome == "stale_profile":
        pieces[0]["profiles"] = [[]]
    if outcome in ("unqualified_schema", "stale_profile"):
        with pytest.raises(ValueError, match="schema|profile"):
            placement.place_room_finishes(document, pieces)
        assert events == [] and created == []
        return
    receipt = placement.place_room_finishes(document, pieces)
    if outcome == "committed":
        assert events == ["started", "committed"] and receipt["status"] == "ok"
        assert [item["piece_key"] for item in receipt["created"]] == [piece["piece_key"] for piece in pieces]
        assert [json.loads(item.record.value)["kind"] for item in created] == ["floor", "ceiling", "wall"]
        assert created[-1].values["bounding"] == 0 and created[-1].joins == [False, False]
    else:
        assert receipt["target_rollback_verified"] is True and events == ["started", "rolled-back"] and created == []
        assert sorted(document.elements) == [10, 20, 30, 40, 50]
