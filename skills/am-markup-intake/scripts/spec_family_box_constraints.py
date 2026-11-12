# -*- coding: utf-8 -*-
"""Revit constraints for the disposable PDF equipment box probe."""

from System import Int64
from pyrevit.api import DB


TOLERANCE_FT = 1e-7
TYPE_NAME = "Archi Magic Equipment Fixture"
PARAMETER_NAMES = ("Equipment Width", "Equipment Depth", "Equipment Height")


def plan_view(document, value):
    if not isinstance(value, dict) or set(value) != {"id", "uid"}:
        raise ValueError("Supply the reviewed zero-elevation family plan view identity.")
    identifier = value["id"]
    if not isinstance(identifier, (str, type(u""))) or not identifier.isdigit() or not 0 < int(identifier) < 2 ** 63:
        raise ValueError("Use the recorded decimal plan view ID.")
    view = document.GetElement(DB.ElementId(Int64(int(identifier))))
    if (not isinstance(view, DB.ViewPlan) or view.IsTemplate or str(view.UniqueId) != value["uid"]
            or view.GenLevel is None or abs(float(view.GenLevel.ProjectElevation)) > TOLERANCE_FT):
        raise ValueError("The reviewed family plan view must be a non-template plan at project zero.")
    return view


def family_parameters(document):
    manager = document.FamilyManager
    return {
        "parameters": sorted((str(parameter.Id.Value), str(parameter.Definition.Name))
                             for parameter in manager.Parameters),
        "types": sorted(str(family_type.Name) for family_type in manager.Types),
        "current_type": str(manager.CurrentType.Name) if manager.CurrentType is not None else None,
    }


def planar_sides(extrusion, minimum, maximum):
    options = DB.Options()
    options.ComputeReferences = True
    sides = (("X", minimum[0]), ("X", maximum[0]), ("Y", minimum[1]), ("Y", maximum[1]))
    matches = {key: [] for key in sides}
    for item in extrusion.get_Geometry(options):
        if not isinstance(item, DB.Solid):
            continue
        for face in item.Faces:
            if not isinstance(face, DB.PlanarFace) or face.Reference is None:
                continue
            normal = face.FaceNormal
            if abs(normal.Z) > TOLERANCE_FT:
                continue
            for axis, coordinate in sides:
                if (abs(abs(getattr(normal, axis)) - 1) <= TOLERANCE_FT
                        and abs(getattr(face.Origin, axis) - coordinate) <= TOLERANCE_FT):
                    matches[(axis, coordinate)].append(face.Reference)
    if any(len(references) != 1 for references in matches.values()):
        raise ValueError("The extrusion did not expose four unique side face references.")
    return [matches[key][0] for key in sides]


def constrain_box(document, view, extrusion, minimum, maximum, parameters, created):
    manager, factory = document.FamilyManager, document.FamilyCreate
    left, front = minimum[:2]
    right, back = maximum[:2]
    endpoints = [
        ((left, front - 2, 0), (left, back + 2, 0)), ((right, front - 2, 0), (right, back + 2, 0)),
        ((left - 2, front, 0), (right + 2, front, 0)), ((left - 2, back, 0), (right + 2, back, 0)),
    ]
    planes = []
    for index, (start, end) in enumerate(endpoints):
        plane = factory.NewReferencePlane(DB.XYZ(*start), DB.XYZ(*end), DB.XYZ.BasisZ, view)
        created.append(plane.Id)
        if index in (0, 2):
            plane.Pinned = True
        planes.append(plane)
    document.Regenerate()
    plane_refs = [plane.GetReference() for plane in planes]
    for plane_ref, face_ref in zip(plane_refs, planar_sides(extrusion, minimum, maximum)):
        constraint = factory.NewAlignment(view, plane_ref, face_ref)
        created.append(constraint.Id)
    dimension_lines = (((left, front - 1, 0), (right, front - 1, 0)),
                       ((left - 1, front, 0), (left - 1, back, 0)))
    for index, endpoints in enumerate(dimension_lines):
        references = DB.ReferenceArray()
        for reference in plane_refs[index * 2:index * 2 + 2]:
            references.Append(reference)
        line = DB.Line.CreateBound(DB.XYZ(*endpoints[0]), DB.XYZ(*endpoints[1]))
        dimension = factory.NewDimension(view, line, references)
        created.append(dimension.Id)
        dimension.FamilyLabel = parameters[index]
    height = extrusion.get_Parameter(DB.BuiltInParameter.EXTRUSION_END_PARAM)
    if height is None or not manager.CanElementParameterBeAssociated(height):
        raise ValueError("Revit did not expose an associable extrusion end parameter.")
    manager.AssociateElementParameterToFamilyParameter(height, parameters[2])


def add_plan_constraints(document, view, extrusion, dimensions, created):
    manager = document.FamilyManager
    existing = family_parameters(document)
    if TYPE_NAME in existing["types"] or any(name in PARAMETER_NAMES for _, name in existing["parameters"]):
        raise ValueError("The blank fixture already contains the probe type or parameters.")
    manager.NewType(TYPE_NAME)
    parameters = [manager.AddParameter(name, DB.GroupTypeId.Geometry, DB.SpecTypeId.Length, False)
                  for name in PARAMETER_NAMES]
    constrain_box(document, view, extrusion, [0, 0, 0], dimensions, parameters, created)
    for parameter, value in zip(parameters, dimensions):
        manager.Set(parameter, float(value))
    document.Regenerate()
    return parameters
