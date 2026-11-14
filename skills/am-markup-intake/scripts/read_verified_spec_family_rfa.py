# -*- coding: utf-8 -*-
"""Read back a saved Revit equipment family and bind the result to its RFA bytes."""

import hashlib
import os

from pyrevit.api import DB

from probe_spec_family_box import AXES, TOLERANCE_FT, specification, verify_bounds
from probe_spec_family_placement import local_rfa_bytes, valid_text
from spec_family_box_constraints import PARAMETER_NAMES
from spec_family_clearance import CATEGORY, clearance_bounds, clearance_offsets
from spec_family_source_parameters import source_values


def find_type(manager, name):
    matches = [family_type for family_type in manager.Types if str(family_type.Name) == name]
    if (len(matches) != 1 or manager.CurrentType is None or str(manager.CurrentType.Name) != name):
        raise ValueError("Reopen the saved family with the exact reviewed type current.")
    return matches[0]


def named_parameter(parameters, name, data_type):
    matches = [parameter for parameter in parameters if str(parameter.Definition.Name) == name]
    if len(matches) != 1 or matches[0].IsInstance or matches[0].Definition.GetDataType() != data_type:
        raise ValueError("The saved type parameter {} is absent, ambiguous or has the wrong type.".format(name))
    return matches[0]


def verify_file_version(document):
    active = DB.Document.GetDocumentVersion(document)
    file_info = DB.BasicFileInfo.Extract(document.PathName)
    try:
        stored = file_info.GetDocumentVersion()
        if (str(active.VersionGUID) != str(stored.VersionGUID)
                or active.NumberOfSaves != stored.NumberOfSaves):
            raise ValueError("The open family and saved RFA have different Revit document versions.")
    finally:
        file_info.Dispose()
    return {"version_guid": str(active.VersionGUID), "number_of_saves": int(active.NumberOfSaves)}


def read_family(document, args):
    if (document is None or not document.IsFamilyDocument or document.IsModified or not document.PathName
            or str(document.Application.VersionNumber) != "2026"):
        raise ValueError("Reopen the saved Revit 2026 RFA without making any changes.")
    if not isinstance(args, dict) or set(args) != {
            "family_name", "type_name", "specification_json", "specification_sha256"}:
        raise ValueError("Supply the reviewed family and type names and confirmed specification.")
    family_name, type_name = args["family_name"], args["type_name"]
    if not valid_text(family_name) or not valid_text(type_name):
        raise ValueError("Supply the exact reviewed family and type names.")
    if os.path.splitext(os.path.basename(document.PathName))[0] != family_name:
        raise ValueError("The saved RFA filename differs from the reviewed project family name.")
    family = document.OwnerFamily
    if (family is None or not family.UniqueId
            or family.FamilyPlacementType != DB.FamilyPlacementType.OneLevelBased
            or family.FamilyCategory is None
            or family.FamilyCategory.Id.Value != DB.ElementId(DB.BuiltInCategory.OST_GenericModel).Value):
        raise ValueError("The RFA is not an unhosted, level-based Generic Model family.")
    digest = hashlib.sha256(local_rfa_bytes(document.PathName)).hexdigest()
    version = verify_file_version(document)
    dimensions, spec = specification(args["specification_json"], args["specification_sha256"])
    offsets = clearance_offsets(spec)
    manager = document.FamilyManager
    family_type = find_type(manager, type_name)
    parameters = list(manager.Parameters)
    for name, expected in zip(PARAMETER_NAMES, dimensions):
        parameter = named_parameter(parameters, name, DB.SpecTypeId.Length)
        value = family_type.AsDouble(parameter)
        if value is None or not abs(float(value) - expected) <= TOLERANCE_FT:
            raise ValueError("The saved type dimensions differ from the confirmed source.")
    metadata = source_values(spec)
    for name, expected in metadata.items():
        parameter = named_parameter(parameters, name, DB.SpecTypeId.String.Text)
        if family_type.AsString(parameter) != expected:
            raise ValueError("The saved type PDF provenance differs from the confirmed source.")
    extrusions, clearance = [], []
    for extrusion in DB.FilteredElementCollector(document).OfClass(DB.Extrusion):
        if len(extrusions) + len(clearance) >= 2 or not extrusion.IsSolid:
            raise ValueError("The saved family contains unexpected or cutting extrusion geometry.")
        if extrusion.Subcategory is not None and str(extrusion.Subcategory.Name) == CATEGORY:
            clearance.append(extrusion)
        else:
            extrusions.append(extrusion)
    if len(extrusions) != 1 or len(clearance) != (1 if offsets is not None else 0):
        raise ValueError("The saved family does not contain the expected equipment and clearance geometry.")
    equipment_bounds = verify_bounds(extrusions[0], [0, 0, 0], dimensions)
    clearance_geometry = None
    if offsets is not None:
        clearance_geometry = verify_bounds(clearance[0], *clearance_bounds(dimensions, offsets))
    if (document.IsModified or verify_file_version(document) != version
            or hashlib.sha256(local_rfa_bytes(document.PathName)).hexdigest() != digest):
        raise ValueError("The active family or RFA bytes changed during verification.")
    return {"status": "verified", "kind": "verified-spec-family-rfa", "family_name": family_name,
            "family_uid": str(family.UniqueId), "type_name": type_name, "rfa_sha256": digest, "rfa_version": version,
            "specification_sha256": args["specification_sha256"], "pdf_source_sha256": metadata["PDF Source SHA256"],
            "source_parameters": metadata, "dimensions_ft": dict(zip(AXES, dimensions)),
            "equipment_geometry": equipment_bounds, "clearance_geometry": clearance_geometry,
            "revit_build": str(document.Application.VersionBuild)}


if "inputs" in globals():
    try:
        result = read_family(doc, inputs)
    except (IOError, KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not verify the saved equipment family.",
                  "error_type": type(error).__name__}
