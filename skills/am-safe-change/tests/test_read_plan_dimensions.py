import json
from pathlib import Path
import runpy
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from plan_dimension_preview import compute


class ElementId:
    def __init__(self, value):
        self.Value = int(value)


class IdList(list):
    Count = property(len)
    Add = list.append

    @classmethod
    def __class_getitem__(cls, _):
        return cls


class Element(NS):
    def IsHidden(self, view):
        return getattr(self, "hidden", False)


class ViewPlan(Element):
    pass


class Grid(Element):
    pass


class Wall(Element):
    pass


class FamilyInstance(Element):
    pass


class Dimension(Element):
    pass


class DimensionType(Element):
    pass


class PlanarFace(NS):
    pass


def point(x, y, z=0):
    return NS(X=x, Y=y, Z=z)


class Line:
    def __init__(self, start, end=None):
        self.IsBound = end is not None
        self.Origin, self.Direction = start, point(-1, 0)
        self.endpoints = [start, end]

    def GetEndPoint(self, index):
        return self.endpoints[index]


def reference(element, name=None):
    return NS(ElementId=element.Id, LinkedElementId=ElementId(-1),
              ConvertToStableRepresentation=lambda document: name or element.UniqueId)


class Collector:
    def __init__(self, document, view_id):
        self.document = document

    def OfClass(self, element_type):
        return (element for element in self.document.elements.values() if isinstance(element, element_type))

    def WherePasses(self, ids):
        return (self.document.GetElement(item) for item in ids if self.document.GetElement(item) is not None)


@pytest.fixture
def runtime(monkeypatch):
    document = NS(
        IsFamilyDocument=False, IsLinked=False, PathName="synthetic.rvt", elements={},
        ProjectInformation=NS(UniqueId="project"),
        Application=NS(VersionNumber="2026", VersionBuild="fixture", ShortCurveTolerance=0.0026),
    )
    document.GetElement = lambda item: document.elements.get(item.Value)
    document.Equals = lambda other: other is document

    def add(element_type, id_value, **fields):
        element = element_type(Id=ElementId(id_value), UniqueId="element-" + str(id_value), **fields)
        document.elements[id_value] = element
        return element

    sketch = NS(Id=ElementId(50), UniqueId="work-plane",
                GetPlane=lambda: NS(Origin=point(0, 0, 25), Normal=point(0, 0, 1)))
    view = add(ViewPlan, 41, Document=document, ViewType="FloorPlan", IsTemplate=False, CropBoxActive=False,
               GetPrimaryViewId=lambda: ElementId(-1), SketchPlane=sketch,
               RightDirection=point(1, 0), UpDirection=point(0, 1), ViewDirection=point(0, 0, 1))
    dimension_type = add(DimensionType, 51, StyleType="Linear")
    for grid_id, x in ((10, 0), (11, 3), (12, 7)):
        grid = add(Grid, grid_id, IsCurved=False)
        grid.GetDatumExtentTypeInView = lambda end, view: "ViewSpecific"
        grid.GetCurvesInView = lambda mode, view, x=x: [Line(point(x, -5), point(x, 5))]

    for wall_id, x, normal in ((20, 1, 1), (21, 8, -1)):
        wall = add(Wall, wall_id, Location=NS(Curve=Line(point(x, -5), point(x, 5))))
        face = PlanarFace(FaceNormal=point(normal, 0), Origin=point(x, 0),
                          EdgeLoops=[[NS(AsCurve=lambda x=x: Line(point(x, -5), point(x, 5)))]])
        wall.faces = [reference(wall)]
        wall.GetGeometryObjectFromReference = lambda ref, face=face: face
        wall.FindInserts = lambda *args, wall_id=wall_id: IdList([ElementId(22)] if wall_id == 20 else [])

    opening = add(
        FamilyInstance, 22, Host=document.elements[20], Category=NS(Id=ElementId(-1)),
        Symbol=NS(Id=ElementId(72), UniqueId="window-symbol"), Mirrored=True, HandFlipped=True, FacingFlipped=False,
        GetTransform=lambda: NS(Origin=point(2, 0), BasisX=point(-1, 0), BasisY=point(0, 1), BasisZ=point(0, 0, 1)),
        GetGeometryObjectFromReference=lambda ref: None,
    )
    opening.GetReferences = lambda role: [reference(opening, role)]
    api = ModuleType("pyrevit.api")
    api.DB = NS(
        ElementId=ElementId, ViewPlan=ViewPlan, ViewType=NS(FloorPlan="FloorPlan"),
        Line=Line, Grid=Grid, Wall=Wall, PlanarFace=PlanarFace, FamilyInstance=FamilyInstance,
        Dimension=Dimension, DimensionType=DimensionType, DimensionStyleType=NS(Linear="Linear"),
        FilteredElementCollector=Collector, ElementIdSetFilter=lambda values: values, Reference=reference,
        DatumEnds=NS(End0=0, End1=1), DatumExtentType=NS(Model="Model", ViewSpecific="ViewSpecific"),
        ShellLayerType=NS(Interior="Interior", Exterior="Exterior"),
        HostObjectUtils=NS(GetSideFaces=lambda wall, side: wall.faces),
        BuiltInCategory=NS(OST_Doors=-2, OST_Windows=-1), FamilyInstanceReferenceType=NS(Left="left", Right="right"),
    )
    system = ModuleType("System")
    system.Int64 = int
    generic = ModuleType("System.Collections.Generic")
    generic.List = IdList
    for name, module in (("System", system), ("System.Collections.Generic", generic), ("pyrevit.api", api)):
        monkeypatch.setitem(sys.modules, name, module)

    def run(**request):
        scope = runpy.run_path(str(SCRIPTS / "read_plan_dimensions.py"), init_globals={
            "doc": document, "uidoc": NS(Document=document, ActiveView=view),
            "inputs": {"request": {"view_id": "41", "dimension_type_id": "51", **request}},
        })
        return scope["result"]

    return run, document, view, dimension_type, add


def row(mode, source_ids, **fields):
    return {"key": mode, "mode": mode, "source_ids": source_ids, "line": [[-1, 0, 25], [9, 0, 25]], **fields}


def read(run, *rows):
    result = run(requests=list(rows))
    assert result["status"] == "ok", result
    return json.loads(result["snapshot_json"])


def test_grid_and_wall_faces_use_the_native_work_plane(runtime):
    run, _, _, _, _ = runtime
    grid = row("grid", ["12", "10", "11"])
    walls = row("overall", ["20", "21"], faces=[{"side": "interior"}, {"side": "exterior"}])
    snapshot = read(run, grid, walls)
    preview, proposals = compute(snapshot)
    assert not preview["blocked"]
    assert snapshot["view"]["sketch_plane"]["uid"] == "work-plane"
    assert snapshot["requests"][0]["witnesses"][0]["trace"] == [[7, -5, 25], [7, 5, 25]]
    assert proposals["grid"]["segments_ft"] == [3, 4]
    assert proposals["overall"]["segments_ft"] == [7]


def test_opening_inventory_and_measured_references_are_bound_to_instance_state(runtime):
    run, document, _, _, _ = runtime
    inventory = run(opening_wall_ids=["20"])
    candidate = inventory["openings"][0]["candidate"]
    opening = row("opening", ["20", "22"])
    initial = read(run, opening)
    assert initial["requests"][0]["opening"] == candidate
    assert "independent trace" in compute(initial)[0]["warnings"][0]["text"]

    opening["opening_evidence"] = {
        "opening": candidate, "traces": [[[6, -1, 25], [6, 1, 25]], [[2, -1, 25], [2, 1, 25]]],
    }
    preview, proposals = compute(read(run, opening))
    assert not preview["blocked"]
    assert proposals["opening"]["segments_ft"] == [4]
    assert [item["stable_ref"] for item in proposals["opening"]["witnesses"]] == ["right", "left"]
    document.elements[22].HandFlipped = False
    assert "stale" in compute(read(run, opening))[0]["warnings"][0]["text"]


def test_unbounded_existing_dimension_is_a_no_op(runtime):
    run, document, _, dimension_type, add = runtime
    add(Dimension, 66, DimensionType=dimension_type, AreReferencesAvailable=True, GetTypeId=lambda: dimension_type.Id,
        References=[reference(document.elements[value]) for value in (12, 11, 10)],
        NumberOfSegments=2, Segments=[NS(Value=4), NS(Value=3)], Curve=Line(point(30, 0, 25)))
    preview, proposals = compute(read(run, row("grid", ["10", "11", "12"])))
    assert preview["after"][0][2] == "Already exists"
    assert proposals == {}


def test_mixed_grid_extents_hidden_elements_and_missing_work_plane_are_reported(runtime):
    run, document, view, _, _ = runtime
    grid = row("grid", ["10", "11", "12"])
    document.elements[10].GetDatumExtentTypeInView = lambda end, view: ("Model", "ViewSpecific")[end]
    assert "mixed" in compute(read(run, grid))[0]["warnings"][0]["text"]
    document.elements[11].hidden = True
    warning = compute(read(run, grid))[0]["warnings"][0]["text"]
    assert "10: The grid has mixed" in warning
    assert "11: Source is unavailable" in warning
    view.SketchPlane = None
    assert run(requests=[grid])["status"] == "stopped"
