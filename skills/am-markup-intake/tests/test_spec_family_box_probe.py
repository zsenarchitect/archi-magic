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
            assert plane == (XYZ.BasisZ, XYZ.Zero)
            return make(SketchPlane, 10)

    class Extrusion:
        pass

    XYZ.BasisZ, XYZ.Zero = XYZ(0, 0, 1), XYZ(0, 0, 0)
    elements, calls = {}, []
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
        assert solid is True and plane.Id.Value == 10
        assert len(profile) == 1 and len(profile[0]) == 4 and profile[0][0][0] == XYZ.Zero
        width, depth = profile[0][1][1].X, profile[0][1][1].Y
        calls.append(("box", width, depth, height))
        extrusion = make(Extrusion, 11)
        extrusion.get_BoundingBox = lambda _: NS(
            Enabled=True, Transform=NS(OfPoint=lambda point: point),
            Min=XYZ.Zero, Max=XYZ(width, depth, height + (1 if fault == "bounds" else 0)))
        return extrusion

    category = -2000151
    db = NS(
        XYZ=XYZ, CurveArray=Array, CurveArrArray=Array, SketchPlane=SketchPlane, Extrusion=Extrusion,
        Plane=NS(CreateByNormalAndOrigin=lambda normal, origin: (normal, origin)),
        Line=NS(CreateBound=lambda start, end: (start, end)),
        FamilyPlacementType=NS(OneLevelBased=1), BuiltInCategory=NS(OST_GenericModel=category),
        ElementId=lambda value: NS(Value=value), FilteredElementCollector=Collector,
        Transaction=Transaction, TransactionStatus=states,
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    document = NS(
        PathName="fixture.rfa", IsFamilyDocument=True, IsReadOnly=False, IsWorkshared=False, IsModelInCloud=False,
        OwnerFamily=NS(UniqueId="fixture", FamilyPlacementType=2 if fault == "hosted" else 1,
                       FamilyCategory=NS(Id=NS(Value=category))),
        Application=NS(VersionNumber="2026", VersionBuild="fixture-build",
                       ShortCurveTolerance=2 if fault == "short" else 0.001),
        FamilyCreate=NS(NewExtrusion=create), Regenerate=lambda: calls.append("regenerate"),
        GetElement=lambda identifier: elements.get(identifier.Value),
    )
    dimensions = {"width": "304.8", "depth": "609.6", "height": "914.4"}
    spec = json.dumps({
        "kind": "confirmed-family-specification", "model_write_approved": False,
        "dimensions_mm": dimensions,
        "source_rows": [{"name": name, "value": value, "unit": "mm"} for name, value in dimensions.items()],
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
