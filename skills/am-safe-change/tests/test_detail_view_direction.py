# -*- coding: utf-8 -*-
"""Issue #257: a detail copy made by scripts/apply_detail_views.py keeps the
look direction of its source section. No Revit needed.

The Revit API documentation (RevitAPI.xml, 2024 and 2026) says:
- ViewSection.CreateDetail: "The view direction of the resulting section
  will be sectionBox.Transform.BasisZ and the up direction will be
  sectionBox.Transform.BasisY. The right hand direction will be computed
  so that (right, up, view direction) form a left handed coordinate
  system." Here "view direction" is the look-into direction.
- View.CropBox: "The X direction of the box is right on screen; Y is up;
  and Z is towards the user." View.ViewDirection: "towards the viewer".
So View.ViewDirection of a new view = -BasisZ of the input box. Passing a
view's CropBox as it is flips the copy. The fake CreateDetail below
follows these sentences.

Run either way:
    python -m pytest skills/am-safe-change/tests -q
    python skills/am-safe-change/tests/test_detail_view_direction.py
"""
import ast
import math
import os
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "apply_detail_views.py")


def load_pure_functions():
    """detail_box_frame and same_direction from the apply script itself."""
    with open(SCRIPT, "rb") as f:
        tree = ast.parse(f.read())
    names = ("detail_box_frame", "same_direction", "step_method", "same_box")
    keep = [n for n in tree.body
            if (isinstance(n, ast.FunctionDef) and n.name in names)
            or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None)
                in ("CREATE_DETAIL", "DUPLICATE"))]
    module = ast.Module(body=keep, type_ignores=[])
    scope = {}
    exec(compile(module, SCRIPT, "exec"), scope)
    return [scope[n] for n in names]


detail_box_frame, same_direction, step_method, same_box = load_pure_functions()


# --- vector helpers -------------------------------------------------------


def add(a, b):
    return [x + y for x, y in zip(a, b)]


def mul(a, k):
    return [x * k for x in a]


def neg(a):
    return [-x for x in a]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def close(a, b, tol=1e-9):
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def to_world(frame, p):
    return add(frame["origin"], add(mul(frame["basis_x"], p[0]),
                                    add(mul(frame["basis_y"], p[1]), mul(frame["basis_z"], p[2]))))


def corners(frame, low, high):
    out = []
    for x in (low[0], high[0]):
        for y in (low[1], high[1]):
            for z in (low[2], high[2]):
                out.append([round(c, 9) for c in to_world(frame, [x, y, z])])
    return sorted(out)


def crop_frame(look, origin=(10.0, 20.0, 3.0)):
    """A section CropBox frame: right, up, towards the viewer (= -look)."""
    up = [0.0, 0.0, 1.0]
    toward = neg(look)
    right = cross(up, toward)  # right x up = toward the viewer (right-handed)
    return {"origin": list(origin), "basis_x": right, "basis_y": up, "basis_z": toward}


def create_detail(frame, low, high):
    """What Revit documents for CreateDetail. Returns the new view's frame."""
    look = frame["basis_z"]
    up = frame["basis_y"]
    right = cross(look, up)  # (right, up, look) left-handed: right x up = -look
    view_direction = neg(look)
    w_min, w_max = to_world(frame, low), to_world(frame, high)
    rel_min, rel_max = [a - b for a, b in zip(w_min, frame["origin"])], \
        [a - b for a, b in zip(w_max, frame["origin"])]
    crop_x = sorted([dot(rel_min, right), dot(rel_max, right)])
    crop_y = sorted([dot(rel_min, up), dot(rel_max, up)])
    return {"right": right, "up": up, "view_direction": view_direction,
            "crop_x": crop_x, "crop_y": crop_y, "far_clip": high[2] - low[2]}


LOOKS = [
    [0.0, 1.0, 0.0],                                                  # north
    [-1.0, 0.0, 0.0],                                                 # west
    [math.cos(math.radians(30)), math.sin(math.radians(30)), 0.0],    # 30 degrees
    [math.cos(math.radians(-117)), math.sin(math.radians(-117)), 0.0],
]
LOW, HIGH = [-3.0, -1.5, -12.0], [7.0, 9.0, 0.0]  # asymmetric on purpose


@pytest.mark.parametrize("look", LOOKS)
def test_box_has_the_same_world_volume_and_is_right_handed(look):
    src = crop_frame(look)
    box = detail_box_frame(src["origin"], src["basis_x"], src["basis_y"], src["basis_z"], LOW, HIGH)
    assert corners(box, box["min"], box["max"]) == corners(src, LOW, HIGH)
    assert close(cross(box["basis_x"], box["basis_y"]), box["basis_z"])  # right-handed
    for axis in ("basis_x", "basis_y", "basis_z"):
        assert abs(dot(box[axis], box[axis]) - 1.0) < 1e-12
    assert all(lo < hi for lo, hi in zip(box["min"], box["max"]))


@pytest.mark.parametrize("look", LOOKS)
def test_copy_keeps_direction_right_and_crop(look):
    src = crop_frame(look)
    box = detail_box_frame(src["origin"], src["basis_x"], src["basis_y"], src["basis_z"], LOW, HIGH)
    new = create_detail(box, box["min"], box["max"])
    assert close(new["view_direction"], src["basis_z"])      # same ViewDirection as the source
    assert close(new["right"], src["basis_x"])               # same right direction
    assert close(new["crop_x"], [LOW[0], HIGH[0]])           # same crop width and position
    assert close(new["crop_y"], [LOW[1], HIGH[1]])
    assert abs(new["far_clip"] - (HIGH[2] - LOW[2])) < 1e-12  # same depth


@pytest.mark.parametrize("look", LOOKS)
def test_the_old_call_with_the_crop_box_as_it_is_flips_the_copy(look):
    src = crop_frame(look)
    old = create_detail(src, LOW, HIGH)
    assert close(old["view_direction"], neg(src["basis_z"]))
    assert not same_direction(old["view_direction"], src["basis_z"])


def test_same_direction():
    assert same_direction([0, 2, 0], [0, 1, 0])
    assert not same_direction([0, 1, 0], [0, -1, 0])
    assert not same_direction([0, 1, 0], [1, 0, 0])
    assert not same_direction([0, 0, 0], [1, 0, 0])


# --- the whole apply script with a fake Revit -----------------------------


class XYZ(object):
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = float(x), float(y), float(z)


class Id(object):
    def __init__(self, value):
        self.Value = int(value)


class Transform(object):
    def __init__(self, src):
        self.Origin, self.BasisX, self.BasisY, self.BasisZ = src.Origin, src.BasisX, src.BasisY, src.BasisZ


class Box(object):
    def __init__(self, frame, low, high):
        self.Transform = NS(Origin=XYZ(*frame["origin"]), BasisX=XYZ(*frame["basis_x"]),
                            BasisY=XYZ(*frame["basis_y"]), BasisZ=XYZ(*frame["basis_z"]))
        self.Min, self.Max = XYZ(*low), XYZ(*high)


def lst(p):
    return [p.X, p.Y, p.Z]


class Section(object):
    def __init__(self, eid, frame, low, high, view_direction):
        self.Id, self._frame, self._low, self._high = Id(eid), frame, low, high
        self.ViewDirection = XYZ(*view_direction)
        self.ViewTemplateId = Id(-1)
        self.Name = "Floor transition A"
        self.can_duplicate = True
        self.duplicate_fake = None

    def CanViewBeDuplicated(self, option):
        return self.can_duplicate

    def Duplicate(self, option):
        return self.duplicate_fake(self, option)

    @property
    def CropBox(self):  # Revit returns a new copy on every read
        return Box(self._frame, self._low, self._high)

    def GetTypeId(self):
        return Id(700)


class Param(object):
    def __init__(self, value=0):
        self.value = value

    def AsInteger(self):
        return self.value

    def Set(self, value):
        self.value = value


class Viewport(object):
    created = []

    def __init__(self, sheet_id, view_id, number):
        self.SheetId, self.ViewId = Id(sheet_id), view_id
        self.LabelOffset, self.LabelLineLength = XYZ(0.01, 0.01, 0), 0.81
        self.BoxCenter, self.param = XYZ(0, 0, 0), Param(number)

    @staticmethod
    def Create(doc, sheet_id, view_id, point):
        vp = Viewport(sheet_id.Value, view_id, 0)
        vp.BoxCenter = point
        doc.viewports.append(vp)
        return vp

    def ChangeTypeId(self, type_id):
        self.type_id = type_id.Value

    def SetBoxCenter(self, point):
        self.BoxCenter = point

    def get_Parameter(self, bip):
        return self.param


class Transaction(object):
    def __init__(self, doc, name):
        self.state = None
        doc.transactions.append(self)

    def Start(self):
        self.state = "started"

    def Commit(self):
        self.state = "committed"

    def RollBack(self):
        self.state = "rolled back"

    def HasStarted(self):
        return self.state is not None

    def HasEnded(self):
        return self.state in ("committed", "rolled back")


def make_fake(monkeypatch, documented=True, method=None, duplicate_shift=0.0):
    look = [math.cos(math.radians(30)), math.sin(math.radians(30)), 0.0]
    src = crop_frame(look)
    source = Section(50, src, LOW, HIGH, src["basis_z"])
    sheet = type("ViewSheet", (), {})()
    doc = NS(viewports=[Viewport(100, Id(11), 10)], transactions=[], boxes=[], created=[],
             views=[source], duplicated=[])
    reference = doc.viewports[0]
    elements = {50: source, 100: sheet, 1: reference}
    doc.GetElement = lambda i: elements.get(i.Value)

    def create_detail_fake(document, type_id, box):
        frame = {"origin": lst(box.Transform.Origin), "basis_x": lst(box.Transform.BasisX),
                 "basis_y": lst(box.Transform.BasisY), "basis_z": lst(box.Transform.BasisZ)}
        doc.boxes.append((frame, lst(box.Min), lst(box.Max)))
        view_direction = neg(frame["basis_z"]) if documented else frame["basis_z"]
        view = NS(Id=Id(900 + len(doc.boxes)), ViewDirection=XYZ(*view_direction), Name="Detail 0")
        doc.created.append(view)
        return view

    def duplicate_fake(view, option):
        """What the field report saw: the same crop, direction and template."""
        assert option == "duplicate option"
        frame = dict(view._frame, origin=add(view._frame["origin"], [duplicate_shift, 0.0, 0.0]))
        copy = Section(950 + len(doc.duplicated), frame, view._low, view._high,
                       lst(view.ViewDirection))
        copy.Name = view.Name + " Copy 1"
        doc.duplicated.append(copy)
        doc.created.append(copy)
        elements[copy.Id.Value] = copy
        return copy.Id

    source.duplicate_fake = duplicate_fake
    db = NS(ElementId=Id, XYZ=XYZ, Transform=Transform, Transaction=Transaction,
            ViewSheet=type(sheet), Viewport=Viewport, ViewSection=type(source), View=object,
            ViewDuplicateOption=NS(Duplicate="duplicate option"),
            BuiltInParameter=NS(VIEWPORT_DETAIL_NUMBER="detail number"),
            FilteredElementCollector=lambda d: NS(
                OfClass=lambda cls: list(d.viewports) if cls is Viewport else list(d.views)))
    db.ViewSection.CreateDetail = staticmethod(create_detail_fake)
    monkeypatch.setitem(sys.modules, "System", NS(Int64=int))
    monkeypatch.setitem(sys.modules, "pyrevit", NS(api=NS(DB=db)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    preview = {"kind": "change-preview", "title": "Add details",
               "params": {"sheet_id": 100, "reference_id": 1, "label_offset": [0.01, 0.01],
                          "label_line_length": 0.81},
               "steps": [{"name": "North transition", "detail_number": 22, "source_view_id": 50,
                          "box_center": [1.5, 2.5], "viewport_type_id": 800,
                          "label_offset": [0.01, 0.01], "label_line_length": 0.81}]}
    if method:
        preview["steps"][0]["method"] = method
    return doc, source, src, preview


def test_apply_script_keeps_the_source_direction(monkeypatch):
    doc, source, src, preview = make_fake(monkeypatch)
    out = runpy.run_path(SCRIPT, init_globals={"doc": doc, "inputs": {"preview": preview}})["result"]
    frame, low, high = doc.boxes[0]
    assert close(frame["basis_z"], neg(src["basis_z"]))          # look-into = source look
    assert corners(frame, low, high) == corners(src, LOW, HIGH)  # same volume
    assert close(lst(doc.created[0].ViewDirection), lst(source.ViewDirection))
    assert out["directions"][0]["same_direction"] is True
    assert out["rows"][0][:2] == ["North transition", 22]
    assert doc.transactions[0].state == "committed"
    assert lst(source.CropBox.Transform.BasisZ) == src["basis_z"]  # the source is untouched


def test_apply_script_stops_when_the_copy_looks_the_other_way(monkeypatch):
    doc, source, src, preview = make_fake(monkeypatch, documented=False)
    with pytest.raises(ValueError) as err:
        runpy.run_path(SCRIPT, init_globals={"doc": doc, "inputs": {"preview": preview}})
    assert "looks the other way" in str(err.value)
    assert doc.transactions[0].state == "rolled back"


def test_step_method():
    assert step_method({"name": "A"}) == "create_detail"
    assert step_method({"name": "A", "method": None}) == "create_detail"
    assert step_method({"name": "A", "method": "duplicate"}) == "duplicate"
    with pytest.raises(ValueError):
        step_method({"name": "A", "method": "copy"})


def test_same_box():
    a = {"origin": [1, 2, 3], "basis_x": [1, 0, 0], "basis_y": [0, 0, 1], "basis_z": [0, -1, 0],
         "min": [-3, -1.5, -12], "max": [7, 9, 0]}
    assert same_box(a, dict(a))
    assert same_box(a, dict(a, origin=[1.00001, 2, 3]))
    assert not same_box(a, dict(a, origin=[1.01, 2, 3]))
    assert not same_box(a, dict(a, basis_z=[0, 1, 0]))
    assert not same_box(a, dict(a, max=[7, 9, 1]))


def test_create_detail_names_the_new_view(monkeypatch):
    doc, source, src, preview = make_fake(monkeypatch)
    out = runpy.run_path(SCRIPT, init_globals={"doc": doc, "inputs": {"preview": preview}})["result"]
    assert doc.created[0].Name == "North transition"
    assert out["directions"][0]["method"] == "create_detail"
    assert out["directions"][0]["same_crop"] is None


def test_duplicate_copies_the_cut(monkeypatch):
    doc, source, src, preview = make_fake(monkeypatch, method="duplicate")
    out = runpy.run_path(SCRIPT, init_globals={"doc": doc, "inputs": {"preview": preview}})["result"]
    assert doc.boxes == []                      # CreateDetail was not called
    copy = doc.duplicated[0]
    assert copy.Name == "North transition"      # not "<source> Copy 1"
    assert close(lst(copy.ViewDirection), lst(source.ViewDirection))
    d = out["directions"][0]
    assert d["method"] == "duplicate" and d["same_direction"] is True and d["same_crop"] is True
    vp = doc.viewports[-1]
    assert vp.ViewId.Value == copy.Id.Value and vp.param.value == 22
    assert out["rows"][0][:2] == ["North transition", 22]
    assert doc.transactions[0].state == "committed"


def test_duplicate_stops_when_the_crop_moved(monkeypatch):
    doc, source, src, preview = make_fake(monkeypatch, method="duplicate", duplicate_shift=0.5)
    with pytest.raises(ValueError) as err:
        runpy.run_path(SCRIPT, init_globals={"doc": doc, "inputs": {"preview": preview}})
    assert "another crop box" in str(err.value)
    assert doc.transactions[0].state == "rolled back"


def test_duplicate_stops_before_the_transaction_when_not_allowed(monkeypatch):
    doc, source, src, preview = make_fake(monkeypatch, method="duplicate")
    source.can_duplicate = False
    with pytest.raises(ValueError) as err:
        runpy.run_path(SCRIPT, init_globals={"doc": doc, "inputs": {"preview": preview}})
    assert "cannot be duplicated" in str(err.value)
    assert doc.transactions == []


def test_stops_before_the_transaction_when_the_name_is_used(monkeypatch):
    doc, source, src, preview = make_fake(monkeypatch, method="duplicate")
    source.Name = "North transition"
    with pytest.raises(ValueError) as err:
        runpy.run_path(SCRIPT, init_globals={"doc": doc, "inputs": {"preview": preview}})
    assert "already exists" in str(err.value)
    assert doc.transactions == []


def test_unknown_method_stops_before_the_transaction(monkeypatch):
    doc, source, src, preview = make_fake(monkeypatch, method="copy")
    with pytest.raises(ValueError):
        runpy.run_path(SCRIPT, init_globals={"doc": doc, "inputs": {"preview": preview}})
    assert doc.transactions == []


def test_apply_script_has_no_f_strings():
    with open(SCRIPT, "rb") as f:
        tree = ast.parse(f.read())
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
