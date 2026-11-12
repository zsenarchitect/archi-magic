# -*- coding: utf-8 -*-
"""Test loading and placing one pinned RFA in a disposable project and roll it back."""

import hashlib
import json
import math
import os
import platform
import re
import shutil
import tempfile

from System import Int64
from pyrevit.api import DB

from probe_spec_family_box import AXES
from spec_family_box_constraints import PARAMETER_NAMES


TEXT = (str, type(u""))
MAX_RFA_BYTES = 32 * 1024 * 1024
MAX_FAMILIES = 2000
TOLERANCE_FT = 1e-7


def valid_text(value):
    return isinstance(value, TEXT) and 0 < len(value) <= 160 and value == value.strip() and all(
        ord(character) >= 32 for character in value)


def level_from(document, value):
    if (not isinstance(value, TEXT) or not re.match(r"[1-9][0-9]{0,18}\Z", value)
            or int(value) >= 2 ** 63):
        raise ValueError("Supply a positive decimal fixture level ID.")
    level = document.GetElement(DB.ElementId(Int64(int(value))))
    if not isinstance(level, DB.Level) or not level.UniqueId:
        raise ValueError("The reviewed fixture level was not found.")
    return level


def loaded_families(document):
    families = []
    for index, family in enumerate(DB.FilteredElementCollector(document).OfClass(DB.Family)):
        if index >= MAX_FAMILIES:
            raise ValueError("The fixture exceeds {} loaded families.".format(MAX_FAMILIES))
        families.append(family)
    return families


def fixture(document, level_id, family_name):
    if (document is None or document.IsFamilyDocument or not document.PathName
            or str(document.Application.VersionNumber) != "2026"
            or document.IsReadOnly or document.IsWorkshared or document.IsModelInCloud):
        raise ValueError("Open a saved writable local non-workshared Revit 2026 project fixture.")
    if not valid_text(family_name):
        raise ValueError("Supply the reviewed family name.")
    level = level_from(document, level_id)
    elevation = float(level.ProjectElevation)
    if not -30000 <= elevation <= 30000 or abs(elevation) <= TOLERANCE_FT:
        raise ValueError("The fixture needs a valid nonzero project level elevation.")
    families = loaded_families(document)
    if any(family.Name.lower() == family_name.lower() for family in families):
        raise ValueError("A family with the reviewed name already exists in the fixture.")
    identities = sorted((str(family.Id.Value), str(family.UniqueId), str(family.Name)) for family in families)
    family_digest = hashlib.sha256(json.dumps(identities, ensure_ascii=True).encode("ascii")).hexdigest()
    return {"document_uid": str(document.ProjectInformation.UniqueId), "family_inventory_sha256": family_digest,
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest(),
            "family_name": family_name, "level_id": level_id, "level_uid": str(level.UniqueId),
            "level_project_elevation_ft": elevation}


def local_rfa_bytes(path):
    if (not isinstance(path, TEXT) or not os.path.isabs(path) or path.startswith(("\\\\", "//"))
            or os.path.splitext(path)[1].lower() != ".rfa" or not os.path.isfile(path)):
        raise ValueError("Supply an absolute path to a local RFA file.")
    with open(path, "rb") as stream:
        data = stream.read(MAX_RFA_BYTES + 1)
    if not data or len(data) > MAX_RFA_BYTES:
        raise ValueError("The RFA is empty or exceeds 32 MiB.")
    return data


def load_and_place(document, path, baseline, type_name, coordinates, verified, created):
    if not document.LoadFamilySymbol(path, type_name):
        raise ValueError("Revit did not load the exact reviewed RFA type.")
    families = [family for family in loaded_families(document)
                if family.Name.lower() == baseline["family_name"].lower()]
    if len(families) != 1:
        raise ValueError("The loaded family does not match the reviewed family name.")
    family = families[0]
    created.append(family.Id)
    symbols = [document.GetElement(identifier) for identifier in family.GetFamilySymbolIds()]
    created.extend(symbol.Id for symbol in symbols if symbol is not None)
    candidates = [symbol for symbol in symbols if isinstance(symbol, DB.FamilySymbol)
                  and DB.Element.Name.GetValue(symbol) == type_name]
    if (family.Name != baseline["family_name"] or len(candidates) != 1
            or family.FamilyPlacementType != DB.FamilyPlacementType.OneLevelBased
            or family.FamilyCategory.Id.Value != DB.ElementId(DB.BuiltInCategory.OST_GenericModel).Value):
        raise ValueError("The loaded family, type, placement mode or category differs from the reviewed RFA.")
    symbol = candidates[0]
    measured_dimensions = {}
    for axis, name in zip(AXES, PARAMETER_NAMES):
        parameters = list(symbol.GetParameters(name))
        if (len(parameters) != 1 or parameters[0].StorageType != DB.StorageType.Double
                or parameters[0].Definition.GetDataType() != DB.SpecTypeId.Length):
            raise ValueError("The loaded type lacks the reviewed length parameter {}.".format(name))
        actual = float(parameters[0].AsDouble())
        if not abs(actual - verified["dimensions_ft"][axis]) <= TOLERANCE_FT:
            raise ValueError("The loaded type dimension {} differs from the verified RFA.".format(name))
        measured_dimensions[axis] = actual
    measured_source = {}
    for name, expected in sorted(verified["source_parameters"].items()):
        parameters = list(symbol.GetParameters(name))
        if (len(parameters) != 1 or parameters[0].StorageType != DB.StorageType.String
                or parameters[0].Definition.GetDataType() != DB.SpecTypeId.String.Text):
            raise ValueError("The loaded type lacks the reviewed PDF parameter {}.".format(name))
        actual = parameters[0].AsString()
        if actual != expected:
            raise ValueError("The loaded type PDF parameter {} differs from the verified RFA.".format(name))
        measured_source[name] = actual
    if not symbol.IsActive:
        symbol.Activate()
        document.Regenerate()
    level = level_from(document, baseline["level_id"])
    instance = document.Create.NewFamilyInstance(DB.XYZ(*coordinates), symbol, level,
                                                  DB.Structure.StructuralType.NonStructural)
    created.append(instance.Id)
    document.Regenerate()
    if (instance.Symbol.Id.Value != symbol.Id.Value or instance.LevelId.Value != level.Id.Value
            or not isinstance(instance.Location, DB.LocationPoint)):
        raise ValueError("The candidate placement type, level or location was not verified.")
    point = instance.Location.Point
    actual = [float(point.X), float(point.Y), float(point.Z)]
    expected = coordinates[:2] + [baseline["level_project_elevation_ft"] + coordinates[2]]
    if any(math.isnan(value) or math.isinf(value) or abs(value - target) > TOLERANCE_FT
           for value, target in zip(actual, expected)):
        raise ValueError("The candidate world position differs from the reviewed level-relative placement.")
    return {"family_uid": str(family.UniqueId), "symbol_uid": str(symbol.UniqueId),
            "instance_uid": str(instance.UniqueId), "position_ft": actual,
            "dimensions_ft": measured_dimensions, "source_parameters": measured_source}


def probe(document, args):
    if not isinstance(args, dict) or set(args) != {
            "baseline", "type_name", "rfa_path", "rfa_sha256", "placement_level_relative_ft", "verified_rfa"}:
        raise ValueError("Supply the reviewed project baseline, RFA, type and level-relative placement.")
    baseline, type_name = args["baseline"], args["type_name"]
    coordinates = args["placement_level_relative_ft"]
    if (not valid_text(type_name) or not isinstance(coordinates, list) or len(coordinates) != 3
            or any(type(value) not in (int, float) or not -30000 <= value <= 30000 for value in coordinates)):
        raise ValueError("Supply the exact type name and finite level-relative XYZ in internal feet.")
    if fixture(document, baseline["level_id"], baseline["family_name"]) != baseline:
        raise ValueError("The active project or level differs from the reviewed fixture.")
    verified = args["verified_rfa"]
    if (not isinstance(verified, dict) or verified.get("status") != "verified"
            or verified.get("kind") != "verified-spec-family-rfa"
            or verified.get("family_name") != baseline["family_name"] or verified.get("type_name") != type_name
            or verified.get("rfa_sha256") != args["rfa_sha256"]
            or verified.get("revit_build") != str(document.Application.VersionBuild)
            or not isinstance(verified.get("pdf_source_sha256"), TEXT)
            or not re.match(r"[0-9a-f]{64}\Z", verified["pdf_source_sha256"])):
        raise ValueError("Supply the matching saved RFA verification receipt from this Revit build.")
    dimensions, metadata = verified.get("dimensions_ft"), verified.get("source_parameters")
    required = {"PDF Source File", "PDF Source Pages", "PDF Source SHA256", "PDF Product", "PDF Variant"}
    if (not isinstance(dimensions, dict) or set(dimensions) != set(AXES)
            or any(type(value) not in (int, float) or not 0 < value <= 100 for value in dimensions.values())
            or not isinstance(metadata, dict) or not required.issubset(metadata) or len(metadata) > 37
            or any(not valid_text(name) or not isinstance(value, TEXT) or not 0 < len(value) <= 500
                   for name, value in metadata.items())
            or metadata["PDF Source SHA256"] != verified["pdf_source_sha256"]):
        raise ValueError("The saved RFA verification receipt lacks complete dimensions or PDF provenance.")
    data = local_rfa_bytes(args["rfa_path"])
    if hashlib.sha256(data).hexdigest() != args["rfa_sha256"]:
        raise ValueError("The RFA does not match the reviewed SHA-256.")
    directory = tempfile.mkdtemp(prefix="archi-spec-family-probe-")
    cleanup_error = None
    try:
        path = os.path.join(directory, os.path.basename(args["rfa_path"]))
        with open(path, "wb") as stream:
            stream.write(data)
        transaction = DB.Transaction(document, "Try reviewed equipment family placement and roll back")
        created, failure, result = [], None, None
        try:
            if transaction.Start() != DB.TransactionStatus.Started:
                raise ValueError("Revit did not confirm the placement transaction started.")
            result = load_and_place(document, path, baseline, type_name, coordinates, verified, created)
        except ValueError as error:
            failure = {"message": str(error)}
        except Exception as error:
            failure = {"message": "Revit could not load or place the reviewed type.", "error_type": type(error).__name__}
        finally:
            try:
                state = transaction.GetStatus()
                if state == DB.TransactionStatus.Started:
                    state = transaction.RollBack()
                rolled_back = ((state == DB.TransactionStatus.Uninitialized
                                or state == DB.TransactionStatus.RolledBack == transaction.GetStatus())
                               and all(document.GetElement(identifier) is None for identifier in created)
                               and fixture(document, baseline["level_id"], baseline["family_name"]) == baseline)
            except Exception:
                rolled_back = False
    finally:
        try:
            shutil.rmtree(directory)
        except OSError as error:
            cleanup_error = {"directory": directory, "error_type": type(error).__name__}
    if not rolled_back:
        report = {"status": "transaction_unresolved", "message": "Could not verify fixture rollback after load and placement."}
    elif failure:
        report = dict(failure, status="stopped", rollback_verified=True)
    else:
        report = {"status": "measured", "rollback_verified": True, "placement": result,
                  "fixture": baseline, "rfa_sha256": args["rfa_sha256"],
                  "revit_build": str(document.Application.VersionBuild), "python_engine": platform.python_implementation()}
    if cleanup_error:
        report["temporary_rfa_cleanup_failed"] = cleanup_error
    return report


if "inputs" in globals():
    try:
        if isinstance(inputs, dict) and set(inputs) == {"mode", "level_id", "family_name"} and inputs["mode"] == "read":
            result = {"status": "ok", "baseline": fixture(doc, inputs["level_id"], inputs["family_name"])}
        else:
            result = probe(doc, inputs)
    except (IOError, KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not prepare the placement fixture.",
                  "error_type": type(error).__name__}
