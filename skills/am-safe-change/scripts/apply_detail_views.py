# -*- coding: utf-8 -*-
"""EXECUTE step for the detail-views recipe (run_modify / --mode=dry_run|modify).

inputs: {"preview_path": "C:/.../x.preview.json"}   (forward slashes are fine)
    or  {"preview": {...preview dict...}}

Replays the preview's ordered "steps" inside ONE transaction, so the
user's Keep/Discard in Revit is all-or-nothing. Before it changes
anything it checks that the sheet still exists, that the reference
viewport still has the label settings in the preview, and that every
proposed detail number is still free. If not, it stops with no change.

Each step creates one detail view and one viewport. The step's
"method" says how the view copies its source section:
- "duplicate" (issue #257): View.Duplicate(ViewDuplicateOption.Duplicate)
  copies the source view. In the field report it kept the crop, view
  direction, template, far clip and "Hide at scales coarser than"
  setting, with no flip. The script checks CanViewBeDuplicated before
  the transaction. After the copy it checks that the new view's
  ViewDirection and CropBox are the source's. If not, it stops and
  Revit rolls everything back.
- "create_detail" (the default, when a step has no method):
  ViewSection.CreateDetail copies the source section's crop box, then
  the new view takes the source view's template. CreateDetail reads the
  box BasisZ as the look-into direction, but a view's CropBox BasisZ
  points towards the viewer (issue #257). Passing source.CropBox as it
  is would make the copy look the other way. So the script turns the
  box 180 degrees about its up axis (BasisX and BasisZ reversed, the
  same world volume, still right-handed), and after CreateDetail it
  checks that the new view's ViewDirection is the source's. If not, it
  stops and Revit rolls everything back.
- Both methods: the new view gets the step's name. The script checks
  first that no view has that name. (View.Duplicate names the copy
  "<source> Copy 1", and the verify read finds viewports by view name.)
- Viewport.Create places the viewport, then ChangeTypeId takes the
  reference viewport's type.
- LabelOffset and LabelLineLength are copied from the reference
  viewport. A plain Create leaves odd label settings (issue #50).
- SetBoxCenter moves the box to the requested point. A plain Create
  lands about 1.4 ft from the requested center (issue #50).
- The detail number goes to the viewport's Detail Number parameter.

result = {"rows": [[name, number, [ox, oy], line_length, [cx, cy]], ...]
          for the new viewports after the steps,
          "directions": [{"name", "source_view_id", "method", "view_direction",
                          "source_view_direction", "same_direction",
                          "same_crop"}, ...],   (same_crop: null for create_detail)
          "steps_applied": n, "partial": false, "escaped": bool}
A dry run returns the same result and Revit rolls everything back.
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
import io
import json
import sys

from pyrevit.api import DB

ESCAPE = sys.platform == "cli"  # see read_detail_views.py: IronPython result encoder is ASCII-only


def txt(s):
    if not ESCAPE:
        return s
    out = []
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif ord(c) < 128:
            out.append(c)
        else:
            out.append("\\u%04x" % ord(c))
    return "".join(out)


def eid_int(element_id):
    try:
        return int(element_id.Value)        # Revit 2024+
    except AttributeError:
        return int(element_id.IntegerValue)  # removed in Revit 2026


# DB.ElementId(int) is ambiguous in IronPython (issue #50). Use Int64
# when the System assembly is available, plain int otherwise.
try:
    import System

    def eid(n):
        return DB.ElementId(System.Int64(n))
except ImportError:
    def eid(n):
        return DB.ElementId(n)


def detail_box_frame(origin, basis_x, basis_y, basis_z, low, high):
    """Frame and extents for ViewSection.CreateDetail from a view CropBox.

    Plain lists in, plain lists out (no Revit), so a test can check it.
    A view's CropBox has BasisX = right, BasisY = up and BasisZ = towards
    the viewer. CreateDetail reads BasisZ as the look-into direction. The
    returned box is the crop box turned 180 degrees about its up axis:
    BasisX and BasisZ reversed (right-handed, as BoundingBoxXYZ needs),
    with min and max mirrored in x and z, so it holds the same world
    volume. Revit then computes right = the source's right.
    """
    return {"origin": list(origin),
            "basis_x": [-c for c in basis_x], "basis_y": list(basis_y),
            "basis_z": [-c for c in basis_z],
            "min": [-high[0], low[1], -high[2]],
            "max": [-low[0], high[1], -low[2]]}


def same_direction(a, b, tolerance=1e-6):
    """True when two direction vectors point the same way."""
    la = sum(c * c for c in a) ** 0.5
    lb = sum(c * c for c in b) ** 0.5
    if la == 0 or lb == 0:
        return False
    return sum(x * y for x, y in zip(a, b)) / (la * lb) >= 1.0 - tolerance


CREATE_DETAIL = "create_detail"
DUPLICATE = "duplicate"


def step_method(step):
    """The copy method of one preview step. No method means create_detail."""
    method = step.get("method") or CREATE_DETAIL
    if method not in (CREATE_DETAIL, DUPLICATE):
        raise ValueError("Step %s has an unknown copy method %r. Use create_detail or duplicate."
                         % (step.get("name"), method))
    return method


def same_box(a, b, tolerance=1e-4):
    """True when two crop boxes are the same, within tolerance (feet).

    a and b are dicts of plain lists: origin, basis_x, basis_y, basis_z,
    min, max (see box_lists).
    """
    for key in ("origin", "basis_x", "basis_y", "basis_z", "min", "max"):
        for x, y in zip(a[key], b[key]):
            if abs(x - y) > tolerance:
                return False
    return True


def coords(p):
    return [p.X, p.Y, p.Z]


def box_lists(box):
    """A BoundingBoxXYZ as a dict of plain lists, for same_box."""
    t = box.Transform
    return {"origin": coords(t.Origin), "basis_x": coords(t.BasisX),
            "basis_y": coords(t.BasisY), "basis_z": coords(t.BasisZ),
            "min": coords(box.Min), "max": coords(box.Max)}


def detail_box_from_crop(crop):
    """A BoundingBoxXYZ for CreateDetail that keeps the source look direction."""
    t = crop.Transform
    plan = detail_box_frame(coords(t.Origin), coords(t.BasisX), coords(t.BasisY),
                            coords(t.BasisZ), coords(crop.Min), coords(crop.Max))
    frame = DB.Transform(t)  # copy: the crop box transform can be read-only
    frame.Origin = DB.XYZ(*plan["origin"])
    frame.BasisX = DB.XYZ(*plan["basis_x"])
    frame.BasisY = DB.XYZ(*plan["basis_y"])
    frame.BasisZ = DB.XYZ(*plan["basis_z"])
    crop.Transform = frame  # crop is a copy (View.CropBox returns a new box)
    crop.Min = DB.XYZ(*plan["min"])
    crop.Max = DB.XYZ(*plan["max"])
    return crop


def detail_number_param(vp):
    """The viewport's Detail Number parameter, or None."""
    param = None
    try:
        param = vp.get_Parameter(DB.BuiltInParameter.VIEWPORT_DETAIL_NUMBER)
    except AttributeError:
        param = None
    if param is None:
        try:
            param = vp.LookupParameter("Detail Number")
        except Exception:
            param = None
    return param


def load_preview():
    if inputs.get("preview"):
        return inputs["preview"]
    path = inputs.get("preview_path")
    if not path:
        raise ValueError("inputs needs preview_path or preview")
    with io.open(path, "r", encoding="utf-8-sig") as f:
        return json.loads(f.read())


preview = load_preview()
if preview.get("kind") != "change-preview":
    raise ValueError("Not a change-preview file.")
if preview.get("blocked"):
    raise ValueError("The preview is BLOCKED. Resolve its blocking warnings first.")
steps = preview.get("steps") or []
if not steps:
    raise ValueError("The preview has no steps. Nothing to apply.")
params = preview.get("params") or {}
sheet_id = params.get("sheet_id")
reference_id = params.get("reference_id")
if sheet_id is None or reference_id is None:
    raise ValueError("The preview has no sheet_id or reference_id.")

sheet = doc.GetElement(eid(sheet_id))
if sheet is None or not isinstance(sheet, DB.ViewSheet):
    raise ValueError("Sheet %s no longer exists." % sheet_id)
reference = doc.GetElement(eid(reference_id))
if reference is None or not isinstance(reference, DB.Viewport):
    raise ValueError("Reference viewport %s no longer exists." % reference_id)

# Stale-state guard: the reference label settings and the free numbers
# must still match the preview. The Detail Number parameter must exist.
want_offset = params.get("label_offset")
want_length = params.get("label_line_length")
off = reference.LabelOffset
if want_offset is not None and (abs(off.X - want_offset[0]) > 1e-9 or abs(off.Y - want_offset[1]) > 1e-9):
    raise ValueError("Model changed since the preview. Reference viewport label offset moved.")
if want_length is not None and abs(reference.LabelLineLength - want_length) > 1e-9:
    raise ValueError("Model changed since the preview. Reference viewport label line length moved.")
if detail_number_param(reference) is None:
    raise ValueError("The viewport Detail Number parameter is not available in this Revit year. "
                     "Check the parameter name with am-revit-api-docs first.")
used = set()
for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport):
    if eid_int(vp.SheetId) != sheet_id:
        continue
    param = detail_number_param(vp)
    if param is not None:
        try:
            used.add(param.AsInteger())
        except Exception:
            pass
for st in steps:
    if st["detail_number"] in used:
        raise ValueError("Model changed since the preview. Detail number %s is now used." % st["detail_number"])

# Copy method and view name guards, before anything changes.
view_names = set()
for v in DB.FilteredElementCollector(doc).OfClass(DB.View):
    try:
        view_names.add(v.Name)
    except Exception:
        pass
for st in steps:
    method = step_method(st)
    if st["name"] in view_names:
        raise ValueError("A view named %s already exists. Give the new detail another name."
                         % txt(st["name"]))
    view_names.add(st["name"])
    if method == DUPLICATE:
        source = doc.GetElement(eid(st["source_view_id"]))
        if source is None or not isinstance(source, DB.ViewSection):
            raise ValueError("Source section %s no longer exists." % st["source_view_id"])
        if not source.CanViewBeDuplicated(DB.ViewDuplicateOption.Duplicate):
            raise ValueError("Source section %s cannot be duplicated. Use the create_detail method."
                             % st["source_view_id"])

t = DB.Transaction(doc, preview.get("title") or "Add detail views")
t.Start()
rows = []
directions = []
done = 0
try:
    for st in steps:
        source = doc.GetElement(eid(st["source_view_id"]))
        if source is None or not isinstance(source, DB.ViewSection):
            raise ValueError("Source section %s no longer exists." % st["source_view_id"])
        method = step_method(st)
        if method == DUPLICATE:
            new_view = doc.GetElement(source.Duplicate(DB.ViewDuplicateOption.Duplicate))
        else:
            box = detail_box_from_crop(source.CropBox)
            new_view = DB.ViewSection.CreateDetail(doc, source.GetTypeId(), box)
        want = coords(source.ViewDirection)
        got = coords(new_view.ViewDirection)
        if not same_direction(want, got):
            raise ValueError("The new detail view for %s looks the other way (view direction %s, "
                             "source %s). Nothing changed." % (txt(st["name"]),
                                                               [round(c, 6) for c in got],
                                                               [round(c, 6) for c in want]))
        same_crop = None
        if method == DUPLICATE:
            same_crop = same_box(box_lists(new_view.CropBox), box_lists(source.CropBox))
            if not same_crop:
                raise ValueError("The duplicate of %s has another crop box than its source. "
                                 "Nothing changed." % txt(st["name"]))
        directions.append({"name": txt(st["name"]), "source_view_id": st["source_view_id"],
                           "method": method,
                           "view_direction": got, "source_view_direction": want,
                           "same_direction": True, "same_crop": same_crop})
        new_view.Name = st["name"]
        if eid_int(source.ViewTemplateId) > 0:
            new_view.ViewTemplateId = source.ViewTemplateId
        cx, cy = st["box_center"][0], st["box_center"][1]
        vp = DB.Viewport.Create(doc, eid(sheet_id), new_view.Id, DB.XYZ(cx, cy, 0))
        vp.ChangeTypeId(eid(st["viewport_type_id"]))
        ox, oy = st["label_offset"][0], st["label_offset"][1]
        vp.LabelOffset = DB.XYZ(ox, oy, 0)
        vp.LabelLineLength = st["label_line_length"]
        vp.SetBoxCenter(DB.XYZ(cx, cy, 0))
        param = detail_number_param(vp)
        if param is None:
            raise ValueError("The new viewport has no Detail Number parameter.")
        param.Set(int(st["detail_number"]))
        # Re-read the values after the change, as the recipe requires.
        center = vp.BoxCenter
        offset = vp.LabelOffset
        rows.append([txt(st["name"]), int(st["detail_number"]),
                     [offset.X, offset.Y], vp.LabelLineLength,
                     [center.X, center.Y]])
        done += 1
    t.Commit()
except Exception:
    if t.HasStarted() and not t.HasEnded():
        t.RollBack()
    raise

result = {"rows": rows, "directions": directions, "steps_applied": done, "partial": False,
          "escaped": ESCAPE}
