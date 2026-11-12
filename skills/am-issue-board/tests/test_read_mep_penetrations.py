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

    def Add(self, other):
        return XYZ(self.X + other.X, self.Y + other.Y, self.Z + other.Z)

    def Normalize(self):
        return self.Multiply(1 / self.GetLength())

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


@pytest.mark.parametrize("outcome", ["through", "outside", "clip", "embedded", "tangent", "oblique", "multiple", "existing_cut"])
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
        ends = [XYZ(-2, 0, 3), XYZ(3, 0, 3)]
    elif outcome == "oblique":
        points[0] = XYZ(1, 2.25, 3)

    count = {"clip": 0, "multiple": 2, "existing_cut": 0}.get(outcome, 1)
    faces = [Face(0), Face(1)]

    def intersect(line, options):
        calls.append("intersect")
        segment = SimpleNamespace(GetEndPoint=lambda index: points[index])
        return SimpleNamespace(SegmentCount=count, GetCurveSegment=lambda index: segment,
                               Dispose=lambda: calls.append("dispose result"))

    db = SimpleNamespace(SolidCurveIntersectionOptions=lambda: SimpleNamespace(
        Dispose=lambda: calls.append("dispose options")))
    frame = faces[0]
    frame.Normal = XYZ(1, 0, 0)
    outer = {"shape": "rectangular", "center": [3, 3], "width": 6, "height": 6, "angle_rad": 0}
    holes = [{"shape": "round", "center": [2, 3], "diameter": 2}] if outcome == "existing_cut" else []
    host = (faces, frame, outer, holes, SimpleNamespace(IntersectWithCurve=intersect), [(0, 1), (0, 6), (0, 6)])
    run = (object(), ends, XYZ(0, 1, 0), run_bounds)
    budget = reader.ReadBudget()
    if outcome == "outside":
        assert reader.measured_pair(db, host, run, 0.25, 0.001, budget) == {"status": "no_crossing", "gross_verified": True}
        assert not calls
    elif outcome == "through":
        assert reader.measured_pair(db, host, run, 0.25, 0.001, budget) == {
            "status": "solid_crossing", "entry_ft": [0, 2, 3], "exit_ft": [1, 2, 3],
            "center_uv_ft": [2, 3], "edge_distance_ft": 2.0, "angle_rad": 0.0, "gross_verified": True}
    elif outcome == "existing_cut":
        result = reader.measured_pair(db, host, run, 0.25, 0.001, budget)
        assert result["status"] == "gross_crossing"
        assert result["existing_cut_verified"]
        assert result["gross_verified"]
    else:
        with pytest.raises(ValueError):
            reader.measured_pair(db, host, run, 0.25, 0.001, budget)
    if outcome in {"through", "clip", "oblique", "multiple", "existing_cut"}:
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


class Line:
    IsBound = True

    def __init__(self, start, end):
        self.points = [start, end]

    def GetEndPoint(self, index):
        return self.points[index]


class Arc:
    def __init__(self, center, radius):
        self.Center = center
        self.Radius = radius
        self.ApproximateLength = 2 * math.pi * radius
        self.Normal = XYZ(1, 0, 0)


def test_existing_cut_profile_reads_round_and_rectangular_trim_in_one_frame():
    face = Face(0)
    face.Normal = XYZ(1, 0, 0)
    db = SimpleNamespace(Line=Line, Arc=Arc)
    outer = [Line(a, b) for a, b in face.outline()]
    a, b, c, d = [XYZ(0, u, v) for u, v in ((1, 1), (2, 1), (2, 2), (1, 2))]
    inner = [Line(a, b), Line(b, c), Line(c, d), Line(d, a)]
    circle = [Arc(XYZ(0, 4, 4), 0.5)]
    profile, _, holes = reader.host_face_profiles(db, [circle, inner, outer], face, 0.001, reader.ReadBudget())
    assert reader.area(profile) == 36
    assert sorted(reader.area(hole) for hole in holes) == [pytest.approx(math.pi / 4), 1]
    assert reader.margin(profile, [1, 2]) == pytest.approx(1)
    with pytest.raises(ValueError, match="overlap"):
        reader.host_face_profiles(db, [circle, circle, outer], face, 0.001, reader.ReadBudget())


@pytest.mark.parametrize("shallow", [False, True])
def test_cylindrical_cut_requires_full_depth_and_complete_side_area(shallow):
    class Planar:
        def __init__(self, origin, normal, curves=()):
            self.Origin = origin
            self.FaceNormal = normal
            self.EdgeLoops = [[SimpleNamespace(AsCurve=lambda curve=curve: curve) for curve in curves]]

    class Cylinder:
        Axis = XYZ(1, 0, 0)
        Origin = XYZ(0, 3, 3)
        Area = math.pi
        EdgeLoops = [[SimpleNamespace(AsCurve=lambda x=x: Arc(XYZ(x, 3, 3), 0.5))
                      for x in (0, 0.5 if shallow else 1)]]

        def get_Radius(self, index):
            return XYZ(0, 0.5, 0) if index == 0 else XYZ(0, 0, 0.5)

    frame = Face(0)
    frame.Normal = XYZ(1, 0, 0)
    db = SimpleNamespace(Line=Line, Arc=Arc, PlanarFace=Planar, CylindricalFace=Cylinder)
    ends = [Planar(XYZ(x, 0, 0), XYZ(direction, 0, 0)) for x, direction in ((0, -1), (1, 1))]
    sides = []
    for a, b in frame.outline():
        c, d = b.Add(XYZ(1, 0, 0)), a.Add(XYZ(1, 0, 0))
        sides.append(Planar(a, XYZ(0, 1, 0), [Line(a, b), Line(b, c), Line(c, d), Line(d, a)]))
    solid = SimpleNamespace(Faces=[*ends, *sides, Cylinder()])
    outer = {"shape": "rectangular", "center": [3, 3], "width": 6, "height": 6, "angle_rad": 0}
    hole = {"shape": "round", "center": [3, 3], "diameter": 1}
    args = (db, solid, ends, frame, 1, outer, [hole], 0.001, reader.ReadBudget())
    if shallow:
        with pytest.raises(ValueError, match="full host depth"):
            reader.verify_prism(*args)
    else:
        reader.verify_prism(*args)
