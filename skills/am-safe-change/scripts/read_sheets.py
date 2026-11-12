# -*- coding: utf-8 -*-
"""READ step for the sheet renumber recipe. Read-only (run_query / --mode=query).

Returns every ViewSheet in the active document, placeholders included,
because placeholders also hold a sheet number that must stay unique.

result = {"document": <title>, "count": N,
          "sheets": [{"id", "number", "name", "placeholder"}, ...]}
Runs under IronPython 2.7 and CPython 3 (no f-strings).
"""
from pyrevit.api import DB


def eid_int(element_id):
    # Revit 2024+ has ElementId.Value (Int64); IntegerValue was removed in 2026.
    try:
        return int(element_id.Value)
    except AttributeError:
        return int(element_id.IntegerValue)


import sys

# The agent runtime's IronPython JSON encoder fails on non-ASCII text
# ("'unknown' codec can't decode byte 0xe9"). Under IronPython, escape text to
# ASCII ("\\" -> "\\\\", "é" -> "\\u00e9") and set "escaped": true.
# sheet_shift.load_read_result() reverses it. CPython returns plain text.
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


def sheet_inventory(document):
    rows = []
    for sheet in DB.FilteredElementCollector(document).OfClass(DB.ViewSheet):
        rows.append({"id": eid_int(sheet.Id), "number": txt(sheet.SheetNumber),
                     "name": txt(sheet.Name), "placeholder": bool(sheet.IsPlaceholder)})
    rows.sort(key=lambda row: row["number"])
    return {"document": txt(document.Title), "count": len(rows), "escaped": ESCAPE, "sheets": rows}


def revision_scope(values, limit):
    import re
    from System import Int64

    if not isinstance(values, list) or len(values) > limit:
        raise ValueError("Supply a bounded list of reviewed element IDs.")
    ids = {}
    for value in values:
        if (not isinstance(value, (str, type(u""))) or not re.match(r"^[1-9][0-9]{0,18}\Z", value)
                or int(value) >= 2**63 or value in ids):
            raise ValueError("Use distinct positive decimal-string Revit IDs.")
        ids[value] = DB.ElementId(Int64(int(value)))
    return ids


def revision_inventory(document, request):
    import hashlib
    from System.Diagnostics import Stopwatch

    if document is None or document.IsFamilyDocument or not document.PathName or document.ProjectInformation is None:
        raise ValueError("Open a saved Revit project before reading sheet revisions.")
    project_uid = document.ProjectInformation.UniqueId
    if not project_uid:
        raise ValueError("The saved project identity is unavailable.")
    ids = revision_scope(request["revision_sheet_ids"], 30)
    if not ids:
        raise ValueError("Select at least one real sheet for revision review.")
    revisions = revision_scope(request.get("revision_ids", []), 50)
    explicit_revision_ids = sorted(revisions, key=int)
    sheets, scheduled_clouds, revisions_by_sheet = {}, {}, {}
    clock = Stopwatch.StartNew()
    for key, identifier in sorted(ids.items(), key=lambda item: int(item[0])):
        if clock.ElapsedMilliseconds >= 10000:
            raise ValueError("The sheet revision inventory did not finish within its limits.")
        sheet = document.GetElement(identifier)
        if not isinstance(sheet, DB.ViewSheet) or sheet.IsPlaceholder or not sheet.UniqueId:
            raise ValueError("A selected sheet is missing or is a placeholder.")
        if not hasattr(sheet, "GetAllRevisionCloudIds"):
            raise ValueError("This Revit version cannot report exact scheduled revision clouds.")
        actual = list(sheet.GetAllRevisionIds())
        explicit = {str(eid_int(value)): value for value in sheet.GetAdditionalRevisionIds()}
        cloud_ids = {str(eid_int(value)) for value in sheet.GetAllRevisionCloudIds()}
        if len(actual) > 500 or len(explicit) > 500 or len(cloud_ids) > 10000:
            raise ValueError("The sheet revision inventory exceeds its limits.")
        ordered = [str(eid_int(value)) for value in actual]
        revisions_by_sheet[key] = set(ordered)
        if len(revisions_by_sheet[key]) != len(ordered) or not set(explicit) <= revisions_by_sheet[key]:
            raise ValueError("The ordered sheet schedule and manually added revisions disagree.")
        revisions.update(explicit)
        revisions.update(zip(ordered, actual))
        sheets[key] = {"id": key, "uid": str(sheet.UniqueId), "number": sheet.SheetNumber,
                       "name": sheet.Name, "additional_revision_ids": sorted(explicit, key=int),
                       "scheduled": [{"revision_id": value, "number": sheet.GetRevisionNumberOnSheet(native)}
                                     for value, native in zip(ordered, actual)],
                       "scheduled_cloud_ids": sorted(cloud_ids, key=int), "clouds": []}
        for cloud_id in cloud_ids:
            scheduled_clouds.setdefault(cloud_id, set()).add(key)
    seen_clouds = set()
    cloud_rows = 0
    comment_chars = 0
    collector = DB.FilteredElementCollector(document)
    try:
        for index, cloud in enumerate(collector.OfClass(DB.RevisionCloud)):
            if index >= 10000 or clock.ElapsedMilliseconds >= 10000:
                raise ValueError("The revision cloud inventory did not finish within its limits.")
            key = str(eid_int(cloud.Id))
            actual_sheets = scheduled_clouds.get(key, set())
            owner = str(eid_int(cloud.OwnerViewId))
            potential = {str(eid_int(value)) for value in cloud.GetSheetIds()}
            if owner in sheets:
                potential.add(owner)
            matching = (potential & set(sheets)) | actual_sheets
            if not matching:
                continue
            revision_id = str(eid_int(cloud.RevisionId))
            revisions[revision_id] = cloud.RevisionId
            comments = cloud.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            comment = comments.AsString() if comments is not None and comments.StorageType == DB.StorageType.String else None
            if not cloud.UniqueId:
                raise ValueError("A revision cloud has no stable identity.")
            cloud_rows += len(matching)
            comment_chars += len(comment or "") * len(matching)
            if cloud_rows > 5000 or comment_chars > 200000:
                raise ValueError("The selected sheet and cloud comments exceed the snapshot size limit.")
            for sheet_id in matching:
                sheet = sheets[sheet_id]
                sheet["clouds"].append({"id": key, "uid": str(cloud.UniqueId), "revision_id": revision_id,
                                        "owner_view_id": owner, "comments": comment,
                                        "scheduled": sheet_id in actual_sheets,
                                        "revision_on_sheet": revision_id in revisions_by_sheet[sheet_id]})
            seen_clouds.add(key)
    finally:
        collector.Dispose()
    if clock.ElapsedMilliseconds >= 10000 or set(scheduled_clouds) - seen_clouds:
        raise ValueError("The sheet's scheduled cloud inventory could not be reconciled.")
    catalog = []
    if len(revisions) > 5000:
        raise ValueError("The referenced revision inventory exceeds its limit.")
    for key, identifier in sorted(revisions.items(), key=lambda item: int(item[0])):
        if clock.ElapsedMilliseconds >= 10000:
            raise ValueError("The referenced revision inventory did not finish within its limits.")
        revision = document.GetElement(identifier)
        if not isinstance(revision, DB.Revision) or not revision.UniqueId:
            raise ValueError("A requested or referenced revision could not be identified.")
        catalog.append({"id": key, "uid": str(revision.UniqueId), "sequence": int(revision.SequenceNumber),
                        "description": revision.Description, "date": revision.RevisionDate,
                        "issued": bool(revision.Issued)})
    for sheet in sheets.values():
        sheet["clouds"].sort(key=lambda row: int(row["id"]))
    return {"kind": "sheet-revision-snapshot", "host_identity": {
                "project_uid": str(project_uid),
                "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()},
            "revision_ids_requested": explicit_revision_ids,
            "sheets": [sheets[key] for key in sorted(sheets, key=int)], "revisions": catalog}


if "doc" in globals() and "inputs" in globals() and "revision_sheet_ids" in inputs:
    import json

    try:
        snapshot = revision_inventory(doc, inputs)
        encoded = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("The sheet revision snapshot exceeds its size limit.")
        result = {"status": "ok", "snapshot_json": encoded}
    except ValueError as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not read the reviewed revision scope.",
                  "error_type": type(error).__name__}
elif "doc" in globals():
    result = sheet_inventory(doc)
