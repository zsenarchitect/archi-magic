# -*- coding: utf-8 -*-
"""READ step for the detail-views recipe. Read-only (run_query / --mode=query).

Returns every ViewSheet with its viewports, plus every section view that
can serve as a source for a new detail. The compute step needs the
viewport label settings and the detail numbers that are already on the
sheet. inputs may scope the read: {"sheet_id": <element id>}.
Without inputs, all sheets are read.

The detail number is read from the viewport's Detail Number parameter.
When Revit does not expose it under that name, detail_number is None
and the compute step blocks the preview with a clear warning.

result = {"document": <title>,
          "sheets": [{"id", "number", "name",
                      "viewports": [{"id", "view_id", "view_name",
                                     "detail_number", "viewport_type_id",
                                     "label_offset": [x, y],
                                     "label_line_length": number,
                                     "box_center": [x, y, z]}, ...]}, ...],
          "sections": [{"id", "name"}, ...]}
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
import sys

from pyrevit.api import DB


def eid_int(element_id):
    # Revit 2024+ has ElementId.Value (Int64); IntegerValue was removed in 2026.
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


# The agent runtime's IronPython JSON encoder fails on non-ASCII text
# ("'unknown' codec can't decode byte 0xe9"). Under IronPython, escape text to
# ASCII ("\\" -> "\\\\", "é" -> "\\u00e9") and set "escaped": true.
# detail_views.unescape() reverses it. CPython returns plain text.
ESCAPE = sys.platform == "cli"


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


def detail_number_of(vp):
    """The viewport's detail number, or None when it is not readable."""
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
    if param is None:
        return None
    try:
        return param.AsInteger()
    except Exception:
        pass
    try:
        return int(param.AsString())
    except Exception:
        return None


want_sheet = None
try:
    want_sheet = int(inputs.get("sheet_id"))
except Exception:
    want_sheet = None

views = {}
for v in DB.FilteredElementCollector(doc).OfClass(DB.View):
    views[eid_int(v.Id)] = v

sheets = []
for vs in DB.FilteredElementCollector(doc).OfClass(DB.ViewSheet):
    sid = eid_int(vs.Id)
    if want_sheet is not None and sid != want_sheet:
        continue
    vps = []
    for vp in DB.FilteredElementCollector(doc).OfClass(DB.Viewport):
        if eid_int(vp.SheetId) != sid:
            continue
        center = vp.BoxCenter
        offset = vp.LabelOffset
        view = views.get(eid_int(vp.ViewId))
        vps.append({
            "id": eid_int(vp.Id),
            "view_id": eid_int(vp.ViewId),
            "view_name": txt(view.Name) if view is not None else "",
            "detail_number": detail_number_of(vp),
            "viewport_type_id": eid_int(vp.GetTypeId()),
            "label_offset": [offset.X, offset.Y],
            "label_line_length": vp.LabelLineLength,
            "box_center": [center.X, center.Y, center.Z],
        })
    sheets.append({
        "id": sid,
        "number": txt(vs.SheetNumber),
        "name": txt(vs.Name),
        "viewports": vps,
    })

sections = []
for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewSection):
    sections.append({"id": eid_int(v.Id), "name": txt(v.Name)})

result = {"document": txt(doc.Title),
          "sheets": sheets,
          "sections": sections,
          "escaped": ESCAPE}
