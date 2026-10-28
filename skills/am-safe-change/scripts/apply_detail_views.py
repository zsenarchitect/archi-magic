# -*- coding: utf-8 -*-
"""EXECUTE step for the detail-views recipe (run_modify / --mode=dry_run|modify).

inputs: {"preview_path": "C:/.../x.preview.json"}   (forward slashes are fine)
    or  {"preview": {...preview dict...}}

Replays the preview's ordered "steps" inside ONE transaction, so the
user's Keep/Discard in Revit is all-or-nothing. Before it changes
anything it checks that the sheet still exists, that the reference
viewport still has the label settings in the preview, and that every
proposed detail number is still free. If not, it stops with no change.

Each step creates one detail view and one viewport:
- ViewSection.CreateDetail copies the source section's crop box, then
  the new view takes the source view's template.
- Viewport.Create places the viewport, then ChangeTypeId takes the
  reference viewport's type.
- LabelOffset and LabelLineLength are copied from the reference
  viewport. A plain Create leaves odd label settings (issue #50).
- SetBoxCenter moves the box to the requested point. A plain Create
  lands about 1.4 ft from the requested center (issue #50).
- The detail number goes to the viewport's Detail Number parameter.

result = {"rows": [[name, number, [ox, oy], line_length, [cx, cy]], ...]
          for the new viewports after the steps,
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

t = DB.Transaction(doc, preview.get("title") or "Add detail views")
t.Start()
rows = []
done = 0
try:
    for st in steps:
        source = doc.GetElement(eid(st["source_view_id"]))
        if source is None or not isinstance(source, DB.ViewSection):
            raise ValueError("Source section %s no longer exists." % st["source_view_id"])
        new_view = DB.ViewSection.CreateDetail(doc, source.GetTypeId(), source.CropBox)
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

result = {"rows": rows, "steps_applied": done, "partial": False, "escaped": ESCAPE}
