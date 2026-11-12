import hashlib
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_spec_family_box.py"


def setup(monkeypatch, fault=None):
    class XYZ(NS):
        def __init__(self, x, y, z):
            super().__init__(X=x, Y=y, Z=z)

    class Array(list):
        Append = list.append

    class SketchPlane:
        @staticmethod
        def Create(document, plane):
            assert plane[0] == XYZ.BasisZ
            return make(SketchPlane, 10 + len(elements))

    class Extrusion:
        pass

    class ViewPlan(NS):
        pass

    class Solid(NS):
        pass

    class Face(NS):
        pass

    XYZ.BasisZ, XYZ.Zero = XYZ(0, 0, 1), XYZ(0, 0, 0)
    elements, calls = {}, []
    manager = NS(Parameters=[], Types=[], CurrentType=None)
    geometries, strings, numbers, formulas, categories = [], {}, {}, {}, []
    view = ViewPlan(Id=NS(Value=20), UniqueId="plan-uid", IsTemplate=False, GenLevel=NS(ProjectElevation=0))
    states = NS(Uninitialized="new", Started="started", RolledBack="rolled_back")

    def make(cls, number):
        item = cls()
        item.Id = NS(Value=number)
        elements[number] = item
        return item

    class Transaction:
        state = states.Uninitialized

        def __init__(self, *_):
            calls.append("transaction")

        def Start(self):
            self.state = states.Started
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            calls.append("rollback")
            self.state = states.RolledBack
            if fault != "rollback":
                elements.clear()
                manager.Parameters.clear()
                manager.Types.clear()
                manager.CurrentType = None
                if fault != "clearance_rollback":
                    categories.clear()
            return self.state

    class Collector:
        def __init__(self, _):
            pass

        def OfClass(self, cls):
            self.cls = cls
            return self

        def ToElementIds(self):
            class Ids(list):
                @property
                def Count(self):
                    return len(self)
            return Ids(item.Id for item in elements.values() if isinstance(item, self.cls))

    def create(solid, profile, plane, height):
        assert solid is True and isinstance(plane, SketchPlane)
        assert len(profile) == 1 and len(profile[0]) == 4
        start, opposite = profile[0][0][0], profile[0][1][1]
        width, depth = opposite.X - start.X, opposite.Y - start.Y
        calls.append(("box", width, depth, height))
        extrusion = make(Extrusion, 1000 + len(elements))
        extrusion.IsSolid = True
        geometry = [width, depth, height]
        geometries.append(geometry)
        extrusion.get_BoundingBox = lambda _: NS(
            Enabled=True, Transform=NS(OfPoint=lambda point: point), Min=start,
            Max=XYZ(start.X + geometry[0], start.Y + geometry[1],
                    start.Z + geometry[2] + (1 if fault == "bounds" else 0)))
        extrusion.get_Geometry = lambda _: [Solid(Faces=[
            Face(FaceNormal=XYZ(-1, 0, 0), Origin=start, Reference="left"),
            Face(FaceNormal=XYZ(1, 0, 0), Origin=opposite, Reference="right"),
            Face(FaceNormal=XYZ(0, -1, 0), Origin=start, Reference="front"),
            Face(FaceNormal=XYZ(0, 1, 0), Origin=opposite, Reference="back"),
        ])]
        extrusion.get_Parameter = lambda _: "end"
        return extrusion

    def new_type(name):
        manager.CurrentType = NS(Name=name, AsString=lambda parameter: (
            "wrong" if fault == "source_readback" else strings[parameter.Definition.Name]))
        manager.Types.append(manager.CurrentType)

    def add_parameter(name, group, storage, instance):
        assert (group, storage, instance) in (("geometry", "length", False), ("identity", "text", False))
        parameter = NS(Id=NS(Value=30 + len(manager.Parameters)), Definition=NS(Name=name))
        manager.Parameters.append(parameter)
        return parameter

    def set_parameter(parameter, value):
        if isinstance(value, str):
            strings[parameter.Definition.Name] = value
            return
        name = parameter.Definition.Name
        numbers[name] = value
        equipment_names = ("Equipment Width", "Equipment Depth", "Equipment Height")
        if name in equipment_names:
            axis = equipment_names.index(name)
            if fault != "flex_inert" or axis != 0:
                geometries[0][axis] = value
            if fault == "flex_coupled" and axis == 0 and value > 1:
                geometries[0][1] += 0.5
        if fault != "clearance_inert":
            for axis, label in enumerate(("Width", "Depth", "Height")):
                if label in formulas:
                    left, right = formulas[label].split(" + ")
                    geometries[1][axis] = numbers[left] + numbers[right]

    def set_formula(parameter, expression):
        formulas[parameter.Definition.Name.split()[-1]] = expression
        calls.append(("formula", expression))

    manager.NewType, manager.AddParameter, manager.Set = new_type, add_parameter, set_parameter
    manager.SetFormula = set_formula
    manager.CanElementParameterBeAssociated = lambda element: element == "end"
    manager.AssociateElementParameterToFamilyParameter = lambda element, parameter: calls.append(("height", element))

    def native_item():
        return make(NS, len(elements) + 100)

    def reference_plane(start, end, normal, active_view):
        assert normal == XYZ.BasisZ and active_view is view
        result = native_item()
        result.GetReference = lambda: "plane-{}".format(result.Id.Value)
        return result

    def align(active_view, plane, face):
        assert active_view is view
        calls.append(("align", plane, face))
        return native_item()

    def dimension(active_view, line, references):
        assert active_view is view and len(references) == 2
        calls.append(("dimension", tuple(references)))
        return native_item()

    def subcategory(parent, name):
        result = NS(Name=name, Id=NS(Value=-42))
        categories.append(result)
        return result

    category = -2000151
    db = NS(
        XYZ=XYZ, CurveArray=Array, CurveArrArray=Array, SketchPlane=SketchPlane, Extrusion=Extrusion,
        Plane=NS(CreateByNormalAndOrigin=lambda normal, origin: (normal, origin)),
        Line=NS(CreateBound=lambda start, end: (start, end)),
        FamilyPlacementType=NS(OneLevelBased=1), BuiltInCategory=NS(OST_GenericModel=category),
        ElementId=lambda value: NS(Value=value), FilteredElementCollector=Collector,
        Transaction=Transaction, TransactionStatus=states, ViewPlan=ViewPlan, Solid=Solid, PlanarFace=Face,
        Options=NS, ReferenceArray=Array, GroupTypeId=NS(Geometry="geometry", IdentityData="identity"),
        SpecTypeId=NS(Length="length", String=NS(Text="text")),
        BuiltInParameter=NS(EXTRUSION_END_PARAM=1),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    for name in ("spec_family_box_constraints", "spec_family_source_parameters", "spec_family_clearance"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    document = NS(
        PathName="fixture.rfa", IsFamilyDocument=True, IsReadOnly=False, IsWorkshared=False, IsModelInCloud=False,
        OwnerFamily=NS(UniqueId="fixture", FamilyPlacementType=2 if fault == "hosted" else 1,
                       FamilyCategory=NS(Id=NS(Value=category), SubCategories=categories)),
        Application=NS(VersionNumber="2026", VersionBuild="fixture-build",
                       ShortCurveTolerance=2 if fault == "short" else 0.001),
        Settings=NS(Categories=NS(NewSubcategory=subcategory)),
        FamilyCreate=NS(NewExtrusion=create, NewReferencePlane=reference_plane, NewAlignment=align,
                        NewDimension=dimension), FamilyManager=manager,
        Regenerate=lambda: calls.append("regenerate"),
        GetElement=lambda identifier: view if identifier.Value == view.Id.Value else elements.get(identifier.Value),
    )
    dimensions = {"width": "304.8", "depth": "609.6", "height": "914.4"}
    clearance = {"clearance_" + side: value for side, value in (
        ("left", "304.8"), ("right", "609.6"), ("front", "152.4"), ("back", "0"),
        ("below", "304.8"), ("above", "152.4"),
    )} if fault and fault.startswith("clearance") else {}
    spec = json.dumps({
        "kind": "confirmed-family-specification", "model_write_approved": False,
        "dimensions_mm": dimensions,
        "source_rows": [{"name": name, "value": value, "unit": "mm", "source": {"page": 1}}
                        for name, value in dimensions.items()] + [
            {"name": "connector_service", "value": "Supply", "source": {"page": 4}},
            {"name": "parameter_model", "value": "AB-12", "source": {"page": 2}},
        ] + [{"name": name, "value": value, "unit": "mm", "source": {"page": 3}}
             for name, value in clearance.items()],
        "clearance_mm": {name: value for name, value in clearance.items()
                         if fault != "clearance_partial" or name != "clearance_left"},
        "pdf": {"file": "Equipment.pdf" if fault != "source_path" else "../Equipment.pdf", "sha256": "a" * 64},
        "product": "Equipment", "variant": "AB",
        "connectors": {"connector_service": "Supply" if fault != "source_drift" else "Return"},
        "parameters": {"parameter_model": "AB-12"},
    })
    digest = hashlib.sha256(spec.encode("utf-8")).hexdigest()
    args = {"baseline": {"family_uid": "fixture", "sketch_plane_ids": [],
                         "path_sha256": hashlib.sha256(b"fixture.rfa").hexdigest()},
            "specification_json": spec, "specification_sha256": digest}
    if fault == "digest":
        args["specification_json"] += " "
    elif fault == "stale":
        args["baseline"]["family_uid"] = "other"
    return document, args, calls, elements


def test_active_family_probe_measures_reviewed_box_and_rolls_back(monkeypatch):
    document, args, calls, elements = setup(monkeypatch)
    read = runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": {"mode": "read"}})["result"]
    assert read == {"status": "ok", "baseline": args["baseline"]}
    result = runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": args})["result"]
    assert result["status"] == "measured", result
    assert result["geometry"] == {"min_ft": [0, 0, 0], "max_ft": [1, 2, 3]}
    assert calls == ["transaction", ("box", 1, 2, 3), "regenerate", "rollback"]
    assert result["rollback_verified"] and not elements


@pytest.mark.parametrize(("fault", "message"), [
    ("digest", "reviewed digest"), ("stale", "reviewed disposable fixture"),
    ("hosted", "unhosted"), ("short", "short-curve"),
])
def test_invalid_fixture_or_spec_cannot_start_a_transaction(monkeypatch, fault, message):
    document, args, calls, elements = setup(monkeypatch, fault)
    result = runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": args})["result"]
    assert result["status"] == "stopped" and message in result["message"]
    assert not calls and not elements


@pytest.mark.parametrize(("fault", "expected"), [("bounds", "stopped"), ("rollback", "transaction_unresolved")])
def test_family_probe_distinguishes_failed_geometry_and_failed_rollback(monkeypatch, fault, expected):
    document, args, calls, _ = setup(monkeypatch, fault)
    result = runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": args})["result"]
    assert result["status"] == expected
    assert calls[-1] == "rollback"



def run_constrained(monkeypatch, fault=None):
    document, args, calls, elements = setup(monkeypatch, fault)
    args["constraint_view"] = {"id": "20", "uid": "plan-uid"}
    try:
        result = runpy.run_path(str(SCRIPT), init_globals={"doc": document, "inputs": args})["result"]
    finally:
        for name in ("spec_family_box_constraints", "spec_family_source_parameters", "spec_family_clearance"):
            sys.modules.pop(name, None)
    return result, calls, elements, document.FamilyManager


def test_probe_flexes_each_dimension_separately_and_restores_original_family(monkeypatch):
    result, calls, elements, manager = run_constrained(monkeypatch)
    assert result["status"] == "measured", result
    expected = {"width": [1.1, 2, 3], "depth": [1, 2.2, 3], "height": [1, 2, 3.3]}
    for axis, dimensions in expected.items():
        assert result["flexed_geometry"][axis]["max_ft"] == pytest.approx(dimensions)
    assert [call[-1] for call in calls if isinstance(call, tuple) and call[0] == "align"] == [
        "left", "right", "front", "back",
    ]
    assert sum(1 for call in calls if isinstance(call, tuple) and call[0] == "dimension") == 2
    assert ("height", "end") in calls and calls[-1] == "rollback"
    assert result["source_parameters"] == {
        "PDF Source File": "Equipment.pdf", "PDF Source Pages": "1, 2, 4",
        "PDF Source SHA256": "a" * 64, "PDF Product": "Equipment", "PDF Variant": "AB",
        "PDF Connector service": "Supply", "PDF Value model": "AB-12",
    }
    assert not elements and not manager.Parameters and not manager.Types


@pytest.mark.parametrize("fault", ["flex_inert", "flex_coupled"])
def test_probe_requires_actual_independent_axis_geometry_changes(monkeypatch, fault):
    result, calls, elements, _ = run_constrained(monkeypatch, fault)
    assert result["status"] == "stopped" and "extrusion bounds differ" in result["message"]
    assert result["rollback_verified"] and calls[-1] == "rollback" and not elements



@pytest.mark.parametrize(("fault", "reason"), [
    ("source_path", "PDF basename"), ("source_drift", "disagree with their PDF source rows"),
])
def test_probe_rejects_unreviewed_source_values_before_mutation(monkeypatch, fault, reason):
    result, calls, elements, _ = run_constrained(monkeypatch, fault)
    assert result["status"] == "stopped" and reason in result["message"]
    assert not calls and not elements


def test_probe_rolls_back_if_revit_does_not_report_exact_source_text(monkeypatch):
    result, calls, elements, _ = run_constrained(monkeypatch, "source_readback")
    assert result["status"] == "stopped" and "PDF provenance differs" in result["message"]
    assert result["rollback_verified"] and calls[-1] == "rollback" and not elements



def test_clearance_envelope_preserves_asymmetric_offsets_during_flex(monkeypatch):
    result, calls, elements, _ = run_constrained(monkeypatch, "clearance")
    assert result["status"] == "measured", result
    assert result["clearance_geometry"] == {"min_ft": [-1, -0.5, -1], "max_ft": [3, 2, 3.5]}
    for axis, maximum in (("width", [3.1, 2, 3.5]), ("depth", [3, 2.2, 3.5]), ("height", [3, 2, 3.8])):
        assert result["flexed_clearance"][axis]["min_ft"] == [-1, -0.5, -1]
        assert result["flexed_clearance"][axis]["max_ft"] == pytest.approx(maximum)
    assert ("formula", "Equipment Width + Clearance Width Allowance") in calls
    assert result["source_parameters"]["PDF Source Pages"] == "1, 2, 3, 4"
    assert result["rollback_verified"] and not elements


@pytest.mark.parametrize(("fault", "status", "reason"), [
    ("clearance_partial", "stopped", "Confirm all six"),
    ("clearance_inert", "stopped", "bounds differ"),
    ("clearance_rollback", "transaction_unresolved", "could not be verified"),
])
def test_clearance_probe_rejects_partial_inert_or_unrestored_geometry(monkeypatch, fault, status, reason):
    result, calls, _, _ = run_constrained(monkeypatch, fault)
    assert result["status"] == status and reason in result["message"]
    if fault == "clearance_partial":
        assert not calls
    else:
        assert calls[-1] == "rollback"
