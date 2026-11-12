"""Read selected split geometry and typed instance parameters in Revit 2026."""

from hashlib import sha256
import json
import math
from time import monotonic

from System import Int64
from pyrevit.api import DB

from element_split_preview import MAX_BYTES, MAX_CUTS, MAX_GEOMETRY, MAX_SOURCES, bounded_list, fields, identifier, uid


TOLERANCE_FT = 1e-6
MAX_PARAMETERS = 10_000
MAX_DEPENDENCIES = 1_000


class ReadBudget:
    def __init__(self):
        self.deadline = monotonic() + 10
        self.parameter_text_bytes = 0
        self.counts = {"geometry": 0, "parameters": 0, "dependencies": 0}
        self.limits = {"geometry": MAX_GEOMETRY, "parameters": MAX_PARAMETERS, "dependencies": MAX_DEPENDENCIES}

    def visit(self, kind="geometry"):
        self.counts[kind] += 1
        if self.counts[kind] > self.limits[kind] or monotonic() >= self.deadline:
            raise ValueError("The native split read exceeded its evidence budget.")


def project_identity(document):
    if (document is None or document.IsFamilyDocument or document.IsLinked or document.IsWorkshared
            or document.IsModified or document.IsModifiable or not document.PathName):
        raise ValueError("Open a saved, unchanged, non-workshared host project.")
    return {"project_uid": uid(str(document.ProjectInformation.UniqueId)),
            "path_sha256": sha256(document.PathName.encode("utf-8")).hexdigest()}


def selected(document, row):
    fields(row, {"id", "uid"})
    raw = identifier(row["id"])
    expected_uid = uid(row["uid"])
    element = document.GetElement(DB.ElementId(Int64(int(raw))))
    if element is None or str(element.Id.Value) != raw or str(element.UniqueId) != expected_uid:
        raise ValueError("A selected element or cut no longer matches its reviewed identity.")
    return element


def measure(value):
    value = float(value)
    if not math.isfinite(value) or abs(value) > 1_000_000:
        raise ValueError("The native geometry has an unsupported measurement.")
    return value


def xyz(value):
    return [measure(value.X), measure(value.Y), measure(value.Z)]


def line_points(curve):
    if not isinstance(curve, DB.Line) or not curve.IsBound:
        raise ValueError("Use a straight bounded Revit line.")
    return [xyz(curve.GetEndPoint(index)) for index in (0, 1)]


def identity(element):
    return {"id": identifier(str(element.Id.Value)), "uid": uid(str(element.UniqueId))}


def dependency_rows(document, identifiers, budget):
    rows = {}
    for raw in identifiers:
        budget.visit("dependencies")
        element = document.GetElement(raw)
        if element is None:
            raise ValueError("A native dependency could not be read.")
        item = identity(element)
        rows[item["id"]] = item
    return sorted(rows.values(), key=lambda row: int(row["id"]))


def read_parameters(element, budget):
    reads = {DB.StorageType.String: ("string", "AsString"),
             DB.StorageType.Integer: ("integer", "AsInteger"),
             DB.StorageType.Double: ("double", "AsDouble"),
             DB.StorageType.ElementId: ("element_id", "AsElementId")}
    entries = {}
    for parameter in element.Parameters:
        budget.visit("parameters")
        storage = reads.get(parameter.StorageType)
        if storage is None or parameter.Definition is None:
            raise ValueError("An instance parameter has no readable definition or storage type.")
        kind, getter = storage
        key = "shared:" + str(parameter.GUID) if parameter.IsShared else "parameter:" + str(parameter.Id.Value)
        if key in entries:
            raise ValueError("The instance has a duplicate parameter identity.")
        has_value = bool(parameter.HasValue)
        value = getattr(parameter, getter)() if has_value else None
        if has_value:
            if kind == "element_id":
                value = str(value.Value)
            elif kind == "double":
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError("An instance parameter contains a nonfinite number.")
            elif kind == "integer":
                value = int(value)
            elif value is None or len(str(value)) > 8192:
                raise ValueError("A string instance parameter is unavailable or exceeds its read limit.")
            else:
                value = str(value)
                budget.parameter_text_bytes += len(json.dumps(value, ensure_ascii=True))
                if budget.parameter_text_bytes > MAX_BYTES:
                    raise ValueError("The native parameter text exceeds the evidence budget.")
        entries[key] = {"identity": key, "storage": kind, "has_value": has_value, "value": value,
                        "read_only": bool(parameter.IsReadOnly)}
    if not entries:
        raise ValueError("The instance parameter inventory is empty.")
    return sorted(entries.values(), key=lambda row: row["identity"])


def parameter_value(element, name, storage, getter):
    parameter = element.get_Parameter(getattr(DB.BuiltInParameter, name))
    if parameter is None or parameter.StorageType != storage or not parameter.HasValue:
        raise ValueError("A required level constraint or offset is unreadable.")
    return getattr(parameter, getter)()


def constraint_height(document, element, level_name, offset_name):
    level_id = parameter_value(element, level_name, DB.StorageType.ElementId, "AsElementId")
    level = document.GetElement(level_id)
    if not isinstance(level, DB.Level):
        raise ValueError("The vertical constraint does not identify a current project level.")
    offset = parameter_value(element, offset_name, DB.StorageType.Double, "AsDouble")
    return measure(measure(level.ProjectElevation) + measure(offset))


def model_elevations(element, budget):
    box = element.get_BoundingBox(None)
    if box is None:
        raise ValueError("The element has no model bounds.")
    values = []
    for point in (box.Min, box.Max):
        budget.visit()
        values.append(xyz(box.Transform.OfPoint(point))[2])
    if not box.Transform.IsIdentity or not values[0] < values[1]:
        raise ValueError("Use nonempty axis-aligned model bounds.")
    return values


def vertical_geometry(document, element, wall, budget):
    if wall:
        if (element.WallType.Kind != DB.WallKind.Basic or element.CrossSection != DB.WallCrossSection.Vertical
                or element.SketchId.Value > 0 or not isinstance(element.Location, DB.LocationCurve)):
            raise ValueError("Use a straight vertical basic wall with an unedited profile.")
        for name in ("WALL_TOP_IS_ATTACHED", "WALL_BOTTOM_IS_ATTACHED"):
            if parameter_value(element, name, DB.StorageType.Integer, "AsInteger"):
                raise ValueError("An attached wall needs separate native verification.")
        if any(True for _ in element.FindInserts(True, True, True, True)):
            raise ValueError("The wall contains inserts or embedded elements.")
        axis = line_points(element.Location.Curve)
        if abs(axis[0][2] - axis[1][2]) > TOLERANCE_FT:
            raise ValueError("The wall axis is not horizontal.")
        base = constraint_height(document, element, "WALL_BASE_CONSTRAINT", "WALL_BASE_OFFSET")
        top_id = parameter_value(element, "WALL_HEIGHT_TYPE", DB.StorageType.ElementId, "AsElementId")
        if top_id.Value == DB.ElementId.InvalidElementId.Value:
            height = parameter_value(element, "WALL_USER_HEIGHT_PARAM", DB.StorageType.Double, "AsDouble")
            top = measure(base + measure(height))
        else:
            top = constraint_height(document, element, "WALL_HEIGHT_TYPE", "WALL_TOP_OFFSET")
    else:
        if (not element.CanSplit or element.IsSlantedColumn or element.Symbol.Family.IsInPlace
                or element.SuperComponent is not None or not isinstance(element.Location, (DB.LocationCurve, DB.LocationPoint))):
            raise ValueError("Use an eligible vertical, non-nested column.")
        base = constraint_height(document, element, "FAMILY_BASE_LEVEL_PARAM", "FAMILY_BASE_LEVEL_OFFSET_PARAM")
        top = constraint_height(document, element, "FAMILY_TOP_LEVEL_PARAM", "FAMILY_TOP_LEVEL_OFFSET_PARAM")
        if isinstance(element.Location, DB.LocationCurve):
            a, b = line_points(element.Location.Curve)
            if math.dist(a[:2], b[:2]) > TOLERANCE_FT or any(
                    abs(value - target) > TOLERANCE_FT for value, target in zip(sorted((a[2], b[2])), (base, top))):
                raise ValueError("The column axis does not match its vertical constraints.")
    if top <= base or any(abs(a - b) > TOLERANCE_FT for a, b in zip(model_elevations(element, budget), (base, top))):
        raise ValueError("The model bounds disagree with the candidate vertical constraints.")
    return {"base_ft": base, "top_ft": top}


def horizontal_loop(loops, budget):
    iterator = iter(loops)
    loop = next(iterator, None)
    if loop is None or next(iterator, None) is not None:
        raise ValueError("Use one closed floor loop without holes.")
    starts, ends = [], []
    for curve in loop:
        budget.visit()
        start, end = line_points(curve)
        starts.append(start)
        ends.append(end)
        if len(starts) > 128:
            raise ValueError("The floor has too many profile edges.")
    if len(starts) < 3 or any(math.dist(a, b) > TOLERANCE_FT
                               for a, b in zip(ends, starts[1:] + starts[:1])):
        raise ValueError("The floor boundary is not closed.")
    if any(abs(point[2] - starts[0][2]) > TOLERANCE_FT for point in starts + ends):
        raise ValueError("The floor boundary is not horizontal.")
    return starts


def floor_geometry(document, floor, budget):
    editor = floor.GetSlabShapeEditor()
    if editor is None or editor.IsEnabled:
        raise ValueError("Use an unshaped floor with readable shape status.")
    sketch = document.GetElement(floor.SketchId)
    if not isinstance(sketch, DB.Sketch) or sketch.OwnerId.Value != floor.Id.Value:
        raise ValueError("The floor sketch ownership could not be verified.")
    original = horizontal_loop(sketch.Profile, budget)
    refs = iter(DB.HostObjectUtils.GetTopFaces(floor))
    reference = next(refs, None)
    if reference is None or next(refs, None) is not None:
        raise ValueError("Use a floor with one measurable top face.")
    face = floor.GetGeometryObjectFromReference(reference)
    if not isinstance(face, DB.PlanarFace) or abs(face.FaceNormal.Z - 1) > TOLERANCE_FT:
        raise ValueError("The floor top face is not horizontal.")
    actual = horizontal_loop(face.GetEdgesAsCurveLoops(), budget)
    matches = [index for index, point in enumerate(actual) if math.dist(point[:2], original[0][:2]) <= TOLERANCE_FT]
    if len(actual) != len(original) or len(matches) != 1 or not any(
            all(math.dist(actual[(matches[0] + direction * index) % len(actual)][:2], point[:2]) <= TOLERANCE_FT
                for index, point in enumerate(original)) for direction in (-1, 1)):
        raise ValueError("The native top face does not match the source sketch boundary.")
    return {"elevation_ft": actual[0][2], "boundary_xy_ft": [point[:2] for point in actual]}


def read_cut(document, row, floor, budget):
    budget.visit()
    element = selected(document, row)
    if not floor and isinstance(element, DB.Level):
        return dict(identity(element), kind="level", elevation_ft=measure(element.ProjectElevation))
    if not floor or not isinstance(element, (DB.Grid, DB.ModelLine, DB.DetailLine)):
        raise ValueError("Use reviewed levels for vertical elements or straight grids and lines for floors.")
    grid = isinstance(element, DB.Grid)
    start, end = line_points(element.Curve if grid else element.GeometryCurve)
    if math.dist(start[:2], end[:2]) <= TOLERANCE_FT or abs(start[2] - end[2]) > TOLERANCE_FT:
        raise ValueError("The reviewed floor cut does not define a horizontal plan line.")
    return dict(identity(element), kind="grid" if grid else "line", start_xy_ft=start[:2], end_xy_ft=end[:2])


def read_source(document, row, budget):
    fields(row, {"source", "cuts"})
    element = selected(document, row["source"])
    if element.ViewSpecific or element.Pinned or element.GroupId.Value > 0 or element.DesignOption is not None:
        raise ValueError("The split source is view-specific, pinned, grouped, or in a design option.")
    floor, wall = isinstance(element, DB.Floor), isinstance(element, DB.Wall)
    if floor or wall:
        category = "floor" if floor else "wall"
    elif isinstance(element, DB.FamilyInstance) and element.Category is not None:
        category = {int(DB.BuiltInCategory.OST_Columns): "architectural_column",
                    int(DB.BuiltInCategory.OST_StructuralColumns): "structural_column"}.get(element.Category.Id.Value)
        if category is None:
            raise ValueError("The selected family is not an architectural or structural column.")
    else:
        raise ValueError("Select a supported host wall, column, or floor.")
    element_type = document.GetElement(element.GetTypeId())
    if element_type is None:
        raise ValueError("The native element type could not be read.")
    source, type_info = identity(element), identity(element_type)
    dependencies = dependency_rows(document, element.GetDependentElements(None), budget)
    joins = dependency_rows(document, DB.JoinGeometryUtils.GetJoinedElements(document, element), budget)
    wall_ends = []
    if wall and isinstance(element.Location, DB.LocationCurve):
        for end in (0, 1):
            neighbors = dependency_rows(document, (neighbor.Id for neighbor in element.Location.get_ElementsAtJoin(end)),
                                        budget)
            wall_ends.append({"end": end, "join_allowed": bool(DB.WallUtils.IsWallJoinAllowedAtEnd(element, end)),
                              "neighbors": neighbors})
    cuts = [read_cut(document, cut, floor, budget) for cut in bounded_list(row["cuts"], 1, MAX_CUTS, "cuts per source")]
    geometry = floor_geometry(document, element, budget) if floor else vertical_geometry(document, element, wall, budget)
    return dict(source, type_id=type_info["id"], type_uid=type_info["uid"], category=category, geometry=geometry,
                cuts=cuts), dict(source, parameters=read_parameters(element, budget), dependencies=dependencies,
                                 geometry_joins=joins, wall_end_joins=wall_ends)


def main(document, args):
    project = project_identity(document)
    version = str(document.Application.VersionNumber)
    if version != "2026":
        raise ValueError("Use Revit 2026 for the initial native split qualification.")
    if args == {"describe": True}:
        return {"status": "ok", "host_identity": project, "revit_version": version}
    fields(args, {"host_identity", "scope"})
    if args["host_identity"] != project:
        raise ValueError("The open host is not the reviewed saved project.")
    minimum = measure(document.Application.ShortCurveTolerance)
    if not 0 < minimum <= 1:
        raise ValueError("The project has an unsupported short-curve tolerance.")
    budget = ReadBudget()
    sources = bounded_list(args["scope"], 1, MAX_SOURCES, "split sources")
    geometries, inventories = [], []
    for source in sources:
        geometry, inventory = read_source(document, source, budget)
        geometries.append(geometry)
        inventories.append(inventory)
    if len({row["id"] for row in geometries}) != len(geometries):
        raise ValueError("The native source scope repeats an element.")
    if project_identity(document) != project or monotonic() >= budget.deadline:
        raise ValueError("The saved project or native read budget changed.")
    snapshot = {"kind": "element-split-evidence", "frame": "revit_host_internal_feet", "host_identity": project,
                "minimum_piece_ft": minimum, "elements": geometries}
    encoded = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    details = json.dumps({"kind": "element-split-source-inventory", "basis_sha256": sha256(encoded.encode()).hexdigest(),
                          "revit_version": version, "elements": inventories},
                         ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded) + len(details) > MAX_BYTES:
        raise ValueError("The geometry and source inventory exceed 2 MiB.")
    return {"status": "ok", "snapshot_json": encoded, "inventory_json": details}


try:
    result = main(doc, inputs)  # noqa: F821
except ValueError as error:
    result = {"status": "blocked", "reason": str(error)}
except Exception as error:
    result = {"status": "blocked", "reason": "The native split read could not be completed.", "error_type": type(error).__name__}
