# -*- coding: utf-8 -*-
"""Read, create, or verify named levels and grids through am-safe-change."""
import hashlib
import re

from System import Int64
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

import datum_preview as preview


def key(element_id):
    return str(element_id.Value)


def element_id(value):
    if (not isinstance(value, preview.TEXT) or not re.match(r"[1-9][0-9]{0,18}\Z", value)
            or int(value) >= 2 ** 63):
        raise ValueError("Use a positive decimal datum ID.")
    return DB.ElementId(Int64(int(value)))


def name_of(element):
    return DB.Element.Name.GetValue(element)


def identity(document):
    if document is None or document.IsFamilyDocument or not document.PathName:
        raise ValueError("Open the saved Revit project used for the preview.")
    return {"uid": str(document.ProjectInformation.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}


def xyz(point):
    return [float(point.X), float(point.Y), float(point.Z)]


def grid_geometry(element):
    curve = element.Curve
    if isinstance(curve, DB.Line) and curve.IsBound:
        return sorted(xyz(curve.GetEndPoint(index)) for index in (0, 1))
    return None


def curved_signature(grid):
    curve = grid.Curve
    if curve.IsBound:
        return [xyz(curve.GetEndPoint(0)), xyz(curve.Evaluate(0.5, True)), xyz(curve.GetEndPoint(1))]
    return None


def describe(element):
    row = {"id": key(element.Id), "uid": str(element.UniqueId), "type_id": key(element.GetTypeId())}
    if isinstance(element, DB.Level):
        row.update(kind="level", name=name_of(element), geometry=float(element.ProjectElevation))
    elif isinstance(element, DB.Grid):
        row.update(kind="grid", name=name_of(element), geometry=grid_geometry(element))
        if row["geometry"] is None:
            row["curved_points"] = curved_signature(element)
    else:
        raise ValueError("An expected level or grid is unavailable.")
    return row


def read_snapshot(document, request, known_new=None):
    project = identity(document)
    preview.requested_rows(request)
    clock = Stopwatch.StartNew()
    rows, children = [], set()
    scanned = [0]
    known_new = known_new or set()

    def check_budget():
        if scanned[0] >= preview.MAX_EXISTING + len(known_new) or clock.ElapsedMilliseconds >= 10000:
            raise ValueError("The datum inventory exceeded its read budget.")
        scanned[0] += 1

    for multi in DB.FilteredElementCollector(document).OfClass(DB.MultiSegmentGrid):
        check_budget()
        parts = []
        for identifier in multi.GetGridIds():
            check_budget()
            child = document.GetElement(identifier)
            if not isinstance(child, DB.Grid):
                raise ValueError("Could not read every segment of an existing multi-segment grid.")
            children.add(key(identifier))
            parts.append(describe(child))
        rows.append({"id": key(multi.Id), "uid": str(multi.UniqueId), "kind": "grid",
                     "name": multi.Text, "geometry": None, "segments": sorted(parts, key=lambda row: int(row["id"]))})
    for datum_class in (DB.Level, DB.Grid):
        for element in DB.FilteredElementCollector(document).OfClass(datum_class):
            if key(element.Id) not in children:
                check_budget()
                if key(element.Id) not in known_new:
                    rows.append(describe(element))
            elif clock.ElapsedMilliseconds >= 10000:
                raise ValueError("The datum inventory exceeded its read budget.")
    if clock.ElapsedMilliseconds >= 10000:
        raise ValueError("The datum inventory exceeded its read budget.")
    return {"kind": "datum-snapshot", "project": project, "request": request,
            "existing": sorted(rows, key=lambda row: int(row["id"]))}


def receipt(project, rows):
    payload = preview.canonical({"project": project, "datums": rows})
    return {"status": "ok", "datums_json": payload}


def abort_transaction(txn, status=None, message=None):
    try:
        if status is None:
            status = txn.GetStatus()
        if status == DB.TransactionStatus.Started:
            status = txn.RollBack()
        if status == DB.TransactionStatus.RolledBack:
            return {"status": "stopped", "message": message or "Revit rolled back the datum changes."}
    except Exception:
        return {"status": "transaction_unresolved", "message": "Could not verify datum rollback."}
    return {"status": "transaction_unresolved", "message": "Check the Revit transaction state before continuing."}


def apply(document, plan, shown):
    if document is None or document.IsReadOnly:
        raise ValueError("Open the writable Revit project used for the preview.")
    if shown.get("blocked") is not False or any(warning.get("blocking") for warning in shown.get("warnings", [])):
        raise ValueError("The reviewed datum preview contains blocking warnings.")
    if plan.get("recipe") != "datum_creation" or not plan.get("steps"):
        raise ValueError("There are no approved new datums to create.")
    fresh = preview.compute(read_snapshot(document, plan["request"]))
    fields = ("kind", "keyColumn", "columns", "before", "after", "ownership_ids", "datum_basis")
    if (fresh["plan"] != plan or fresh["preview"]["blocked"]
            or any(shown.get(field) != fresh["preview"][field] for field in fields)):
        raise ValueError("The project or datum inventory changed after preview. Read and preview again.")
    if len(plan["steps"]) > preview.MAX_ROWS:
        raise ValueError("The plan exceeds the datum creation budget.")

    native_name = getattr(getattr(DB, "NamingUtils", None), "IsValidName", None)
    lines = {}
    for index, item in enumerate(plan["steps"]):
        if native_name is not None and not native_name(item["name"]):
            raise ValueError("Revit rejected a proposed datum name.")
        if item["kind"] == "grid":
            start, end = [DB.XYZ(*point) for point in item["geometry"]]
            if (not DB.XYZ.IsWithinLengthLimits(start) or not DB.XYZ.IsWithinLengthLimits(end)
                    or start.DistanceTo(end) <= document.Application.ShortCurveTolerance):
                raise ValueError("A grid line exceeds design limits or falls below Revit's curve tolerance.")
            lines[index] = DB.Line.CreateBound(start, end)
        elif not DB.XYZ.IsWithinLengthLimits(DB.XYZ(0, 0, item["geometry"])):
            raise ValueError("A level elevation exceeds Revit's design limits.")

    txn = DB.Transaction(document, "Create named levels and grids")
    if txn.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the datum transaction."}
    try:
        created = []
        for index, item in enumerate(plan["steps"]):
            datum = (DB.Level.Create(document, item["geometry"]) if item["kind"] == "level"
                     else DB.Grid.Create(document, lines[index]))
            if datum is None:
                raise ValueError("Revit declined to create a requested datum.")
            datum.Name = item["name"]
            created.append(datum)
        document.Regenerate()
        readback = receipt(plan["project"], [describe(item) for item in created])
        preview.verify_contents(plan, readback)
        status = txn.Commit()
        if status != DB.TransactionStatus.Committed:
            return abort_transaction(txn, status)
        return readback
    except Exception as error:
        # The document may be invalid after a failed regeneration.
        return abort_transaction(txn, message=error.args[0] if isinstance(error, ValueError) else None)


def verify(document, plan, created):
    if not isinstance(created, list) or len(created) != len(plan["steps"]) or len(created) > preview.MAX_ROWS:
        raise ValueError("Supply the actual kept datum identities.")
    rows = []
    for item in created:
        element = document.GetElement(element_id(item["id"]))
        if element is None or str(element.UniqueId) != item["uid"]:
            raise ValueError("An exact kept datum is unavailable.")
        rows.append(describe(element))
    result = receipt(identity(document), rows)
    _, created_ids = preview.verify_contents(plan, result)
    fresh = read_snapshot(document, plan["request"], created_ids)
    if preview.digest(fresh) != plan["basis"]:
        raise ValueError("The original datum inventory differs from the preview baseline.")
    return result


def main(document, args):
    mode = args.get("mode", "read")
    if mode == "read":
        snapshot = preview.canonical(read_snapshot(document, args["request"]))
        if len(snapshot) > preview.MAX_SNAPSHOT:
            raise ValueError("The datum snapshot exceeds 2 MiB.")
        return {"status": "ok", "snapshot_json": snapshot}
    if mode == "apply":
        return apply(document, args["plan"], args["preview"])
    if mode == "verify":
        return verify(document, args["plan"], args["created"])
    raise ValueError("Choose read, apply, or verify.")


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": error.args[0]}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not complete the datum operation.",
                  "error_type": type(error).__name__}
