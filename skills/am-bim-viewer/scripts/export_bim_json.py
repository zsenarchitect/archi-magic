# -*- coding: utf-8 -*-
"""Export visible model elements of the active Revit view to am-bim-viewer JSON.

Runs inside Revit on IronPython 2.7 (pyRevit Agent Runtime, query mode):
    pyrevit agent run export_bim_json.py --mode=query --revit=2026

Read-only: it opens no transaction and changes nothing in the model.
It writes one JSON file (schema "am-bim-viewer/1") and prints a one-line
JSON summary with the file path and the counts of what it skipped.

Coordinates are Revit internal coordinates: decimal feet, X = East,
Y = North, Z = Up (project internal origin). Do not swap axes here: the
viewer keeps the same Z-up frame.

Revit API members used were checked for 2024, 2025 and 2026 with the
am-revit-api-docs search script. ElementId.IntegerValue is gone in 2026,
so the id comes from ElementId.Value (present in 2024+).
"""
import codecs
import json
import os

from Autodesk.Revit.DB import (
    BuiltInParameter,
    CategoryType,
    FilteredElementCollector,
    GeometryInstance,
    Mesh,
    Options,
    Solid,
    SpecTypeId,
    StorageType,
    View3D,
    ViewDetailLevel,
    ViewType,
)

# ---- settings -------------------------------------------------------------
# Leave CATEGORIES empty to take every model category visible in the view,
# or list category names, for example ["Walls", "Floors", "Doors"].
CATEGORIES = []
MAX_ELEMENTS = 5000
MAX_TRIANGLES = 1500000
MAX_PARAMS = 40
# "auto": the active view when it is a 3D view; else the 3D view named VIEW3D_NAME
#         (or the first non-template 3D view), read without activating it; else the whole model.
# "view": always the active view. "model": always the whole model.
SOURCE = "auto"
VIEW3D_NAME = "{3D}"
# Leave LEVEL empty for all levels, or give a level name, for example "Level 1".
LEVEL = ""
OUT_PATH = None  # None: %TEMP%\am-bim-viewer\<model title>.json
# ---------------------------------------------------------------------------


def get_uidoc():
    try:
        return __revit__.ActiveUIDocument  # noqa: F821 (pyRevit global)
    except NameError:
        from pyrevit import revit
        return revit.uidoc


def id_value(element_id):
    # Revit 2024+ has ElementId.Value (Int64). IntegerValue was removed in 2026.
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


def length_spec_units(definition):
    try:
        spec = definition.GetDataType()
    except Exception:
        return None
    if spec == SpecTypeId.Length:
        return "ft"
    if spec == SpecTypeId.Area:
        return "sq ft"
    if spec == SpecTypeId.Volume:
        return "cu ft"
    return None


def read_param(param):
    if not param.HasValue:
        return None
    st = param.StorageType
    if st == StorageType.Double:
        unit = length_spec_units(param.Definition)
        if unit:
            return {"v": round(param.AsDouble(), 4), "u": unit}
        return param.AsValueString() or round(param.AsDouble(), 4)
    if st == StorageType.String:
        return param.AsString()
    if st == StorageType.Integer:
        return param.AsValueString() or param.AsInteger()
    if st == StorageType.ElementId:
        return param.AsValueString()
    return None


def element_params(element):
    out = {}
    for param in element.Parameters:
        if len(out) >= MAX_PARAMS:
            break
        try:
            name = param.Definition.Name
            value = read_param(param)
        except Exception:
            continue
        if value is None or value == "":
            continue
        out[name] = value
    return out


class MeshBuilder(object):
    def __init__(self):
        self.vertices = []
        self.indices = []
        self.lookup = {}

    def vertex(self, xyz):
        key = (round(xyz.X, 5), round(xyz.Y, 5), round(xyz.Z, 5))
        index = self.lookup.get(key)
        if index is None:
            index = len(self.vertices) // 3
            self.lookup[key] = index
            self.vertices.extend(key)
        return index

    def add_mesh(self, mesh):
        for i in range(mesh.NumTriangles):
            tri = mesh.get_Triangle(i)
            self.indices.extend([self.vertex(tri.get_Vertex(k)) for k in range(3)])

    def triangle_count(self):
        return len(self.indices) // 3


def walk(geometry, builder):
    for obj in geometry:
        if isinstance(obj, Solid):
            if obj.Faces.Size == 0:
                continue
            for face in obj.Faces:
                mesh = face.Triangulate()
                if mesh is not None:
                    builder.add_mesh(mesh)
        elif isinstance(obj, Mesh):
            builder.add_mesh(obj)
        elif isinstance(obj, GeometryInstance):
            # GetInstanceGeometry is already in the model coordinate system.
            walk(obj.GetInstanceGeometry(), builder)


def type_names(doc, element):
    family, type_name = "", ""
    type_id = element.GetTypeId()
    if type_id is not None and id_value(type_id) > 0:
        el_type = doc.GetElement(type_id)
        if el_type is not None:
            family = getattr(el_type, "FamilyName", "") or ""
            p = el_type.get_Parameter(BuiltInParameter.ALL_MODEL_TYPE_NAME)
            type_name = p.AsString() if p is not None and p.HasValue else ""
    return family, type_name


def level_name(doc, element):
    try:
        level_id = element.LevelId
    except Exception:
        return ""
    if level_id is None or id_value(level_id) <= 0:
        return ""
    level = doc.GetElement(level_id)
    return level.Name if level is not None else ""


def export():
    uidoc = get_uidoc()
    doc = uidoc.Document
    view = uidoc.ActiveView

    use_view = SOURCE == "view" or (
        SOURCE == "auto" and view is not None and view.ViewType == ViewType.ThreeD and not view.IsTemplate)
    if SOURCE == "auto" and not use_view:
        # The active view is a sheet, plan, etc.: read a 3D view by id. It is not opened or
        # activated, so the user's view and selection do not change.
        views = [v for v in FilteredElementCollector(doc).OfClass(View3D) if not v.IsTemplate]
        named = [v for v in views if v.Name == VIEW3D_NAME]
        pick = named[0] if named else (views[0] if views else None)
        if pick is not None:
            view, use_view = pick, True
    options = Options()
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = False
    if use_view:
        options.View = view  # also sets the detail level from the view
        collector = FilteredElementCollector(doc, view.Id)
        source_name = view.Name
    else:
        options.DetailLevel = ViewDetailLevel.Medium
        collector = FilteredElementCollector(doc)
        source_name = "Whole model"
    section_box = False  # a section box in the 3D view limits the export to what is inside it
    if use_view:
        try:
            section_box = bool(view.IsSectionBoxActive)
        except Exception:
            pass
    collector = collector.WhereElementIsNotElementType()
    wanted = set(CATEGORIES)
    elements = []
    skipped = {"no_category": 0, "not_model": 0, "filtered": 0, "no_geometry": 0,
                "errors": 0, "over_element_cap": 0, "over_triangle_cap": 0}
    triangles = 0

    for element in collector:
        category = element.Category
        if category is None:
            skipped["no_category"] += 1
            continue
        if category.CategoryType != CategoryType.Model:
            skipped["not_model"] += 1
            continue
        if wanted and category.Name not in wanted:
            skipped["filtered"] += 1
            continue
        if LEVEL and level_name(doc, element) != LEVEL:
            skipped["filtered"] += 1
            continue
        if len(elements) >= MAX_ELEMENTS:
            skipped["over_element_cap"] += 1
            continue
        try:
            geometry = element.get_Geometry(options)
            if geometry is None:
                skipped["no_geometry"] += 1
                continue
            builder = MeshBuilder()
            walk(geometry, builder)
            if builder.triangle_count() == 0:
                skipped["no_geometry"] += 1
                continue
            if triangles + builder.triangle_count() > MAX_TRIANGLES:
                skipped["over_triangle_cap"] += 1
                continue
            family, type_name = type_names(doc, element)
            elements.append({
                "id": id_value(element.Id),
                "category": category.Name,
                "name": element.Name or "",
                "family": family,
                "type": type_name,
                "level": level_name(doc, element),
                "params": element_params(element),
                "mesh": {"vertices": builder.vertices, "indices": builder.indices},
            })
            triangles += builder.triangle_count()
        except Exception:
            skipped["errors"] += 1

    data = {
        "schema": "am-bim-viewer/1",
        "units": "ft",
        "source": {"title": doc.Title, "view": source_name, "generator": "export_bim_json.py"},
        "elements": elements,
        "annotations": [{"text": "Internal origin", "position": [0, 0, 0]}],
    }

    path = OUT_PATH
    if not path:
        folder = os.path.join(os.environ.get("TEMP", "."), "am-bim-viewer")
        if not os.path.isdir(folder):
            os.makedirs(folder)
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in doc.Title)
        path = os.path.join(folder, safe + ".json")
    # ensure_ascii=False: IronPython's ASCII path tries to .decode() .NET
    # strings and fails on characters such as the degree sign. Write UTF-8.
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    with codecs.open(path, "w", "utf-8") as handle:
        handle.write(text)

    print(json.dumps({"path": path, "source": source_name, "elements": len(elements), "triangles": triangles, "sectionBox": section_box, "skipped": skipped}))


export()
