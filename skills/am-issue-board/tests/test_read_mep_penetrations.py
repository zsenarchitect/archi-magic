from dataclasses import dataclass
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import read_mep_penetrations as reader


@dataclass
class XYZ:
    X: float
    Y: float
    Z: float

    def Subtract(self, other):
        return XYZ(self.X - other.X, self.Y - other.Y, self.Z - other.Z)

    def DotProduct(self, other):
        return self.X * other.X + self.Y * other.Y + self.Z * other.Z

    def Multiply(self, value):
        return XYZ(self.X * value, self.Y * value, self.Z * value)

    def GetLength(self):
        return math.hypot(self.X, self.Y, self.Z)

    def DistanceTo(self, other):
        return self.Subtract(other).GetLength()


class Face:
    def __init__(self, x):
        self.Origin = XYZ(x, 0, 0)
        self.XVector = XYZ(0, 1, 0)
        self.YVector = XYZ(0, 0, 1)

    def Project(self, point):
        return SimpleNamespace(Distance=abs(point.X - self.Origin.X), UVPoint=(point.Y, point.Z))

    def IsInside(self, uv):
        return all(0 <= coordinate <= 6 for coordinate in uv)

    def outline(self):
        a, b, c, d = [XYZ(self.Origin.X, u, v) for u, v in ((0, 0), (6, 0), (6, 6), (0, 6))]
        return [(a, b), (b, c), (c, d), (d, a)]


@pytest.mark.parametrize("outcome", ["through", "outside", "clip", "embedded", "tangent", "oblique", "multiple"])
def test_native_pair_needs_a_full_interior_crossing(outcome):
    calls = []
    ends = [XYZ(-2, 2, 3), XYZ(3, 2, 3)]
    points = [XYZ(1, 2, 3), XYZ(0, 2, 3)]
    run_bounds = [(-2, 3), (1.5, 2.5), (2.5, 3.5)]
    if outcome == "outside":
        run_bounds[1] = (8, 9)
    elif outcome == "embedded":
        ends[0] = XYZ(0.5, 2, 3)
    elif outcome == "tangent":
        points = [XYZ(1, 0, 3), XYZ(0, 0, 3)]
    elif outcome == "oblique":
        points[0] = XYZ(1, 2.25, 3)

    count = {"clip": 0, "multiple": 2}.get(outcome, 1)
    faces = [Face(0), Face(1)]

    def intersect(line, options):
        calls.append("intersect")
        segment = SimpleNamespace(GetEndPoint=lambda index: points[index])
        return SimpleNamespace(SegmentCount=count, GetCurveSegment=lambda index: segment,
                               Dispose=lambda: calls.append("dispose result"))

    db = SimpleNamespace(SolidCurveIntersectionOptions=lambda: SimpleNamespace(
        Dispose=lambda: calls.append("dispose options")))
    host = (faces, [face.outline() for face in faces], XYZ(1, 0, 0),
            SimpleNamespace(IntersectWithCurve=intersect), [(0, 1), (0, 6), (0, 6)])
    run = (object(), ends, XYZ(0, 1, 0), run_bounds)
    budget = reader.ReadBudget()
    if outcome == "outside":
        assert reader.measured_pair(db, host, run, 0.25, 0.001, budget) == {"status": "no_crossing"}
        assert not calls
    elif outcome == "through":
        assert reader.measured_pair(db, host, run, 0.25, 0.001, budget) == {
            "status": "solid_crossing", "entry_ft": [0, 2, 3], "exit_ft": [1, 2, 3],
            "center_uv_ft": [2, 3], "edge_distance_ft": 2.0, "angle_rad": 0.0}
    else:
        with pytest.raises(ValueError):
            reader.measured_pair(db, host, run, 0.25, 0.001, budget)
    if outcome != "outside":
        assert budget.intersections == 1
        assert calls == ["intersect", "dispose result", "dispose options"]


def test_native_call_budget_rejects_the_next_intersection():
    budget = reader.ReadBudget()
    budget.intersections = 999
    budget.intersect()
    with pytest.raises(reader.ReadLimit, match="1,000"):
        budget.intersect()


def test_link_transform_refuses_ambiguous_origin_or_reflection():
    standard = SimpleNamespace(IsConformal=True, HasReflection=False, Scale=1, Origin=XYZ(0, 0, 0),
                               BasisX=XYZ(1, 0, 0), BasisY=XYZ(0, 1, 0), BasisZ=XYZ(0, 0, 1))
    changed = SimpleNamespace(**vars(standard))
    link = SimpleNamespace(GetTransform=lambda: standard, GetTotalTransform=lambda: changed)
    assert reader.qualified_transform(link, 0.001) is standard
    changed.Origin = XYZ(1, 0, 0)
    with pytest.raises(ValueError, match="ambiguous"):
        reader.qualified_transform(link, 0.001)
    changed.Origin = standard.Origin
    standard.HasReflection = True
    with pytest.raises(ValueError, match="reflected"):
        reader.qualified_transform(link, 0.001)
