# -*- coding: utf-8 -*-
"""Create or verify a compound-type drafting view through am-safe-change."""
import hashlib
import math
import os
import sys
from itertools import product

from pyrevit.api import DB
from System.Diagnostics import Stopwatch

import compound_legend_preview as preview
from read_compound_legend import element_name, identifier, key, read_snapshot

sys.path.insert(0, os.path.join(os.path.dirname(preview.__file__), "lib"))
from reference_samples import find_overlaps, layout_rows  # noqa: E402


COORD_TOLERANCE_FT = 1e-6
ROW_GAP_FT = 1.0
SKETCH_GAP_FT = 2.0


def normalized_text(value):
    return value.replace(u"\r\n", u"\n").replace(u"\r", u"\n").rstrip(u"\n")


def point(view, xy):
    return view.Origin.Add(view.RightDirection.Multiply(xy[0])).Add(view.UpDirection.Multiply(xy[1]))


def local(view, xyz):
    offset = xyz.Subtract(view.Origin)
    return (offset.DotProduct(view.RightDirection), offset.DotProduct(view.UpDirection))


def note_bounds(view, note):
    bounds = note.get_BoundingBox(view)
    if bounds is None:
        raise ValueError("A generated layer table has no measurable bounds.")
    points = [
        local(view, bounds.Transform.OfPoint(DB.XYZ(*corner)))
        for corner in product((bounds.Min.X, bounds.Max.X),
                              (bounds.Min.Y, bounds.Max.Y), (bounds.Min.Z, bounds.Max.Z))
    ]
    if any(math.isnan(value) or math.isinf(value) for xy in points for value in xy):
        raise ValueError("A generated layer table has invalid bounds.")
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def line_ends(view, element):
    curve = element.GeometryCurve
    return [local(view, curve.GetEndPoint(index)) for index in (0, 1)]


def same_segment(actual, expected):
    def matches(points):
        return all(abs(a - b) <= COORD_TOLERANCE_FT
                   for found, wanted in zip(points, expected) for a, b in zip(found, wanted))

    return matches(actual) or matches(list(reversed(actual)))


def check_view(view):
    if not isinstance(view, DB.ViewDrafting) or view.IsTemplate:
        raise ValueError("The expected drafting view is unavailable.")
    if key(view.ViewTemplateId) != "-1" or int(view.Scale) != preview.VIEW_SCALE:
        raise ValueError("The drafting view has a template or a scale that differs from the reviewed legend.")


def inspect_view(document, view):
    check_view(view)
    project = {"uid": str(document.ProjectInformation.UniqueId),
               "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}
    contents = []
    for element in DB.FilteredElementCollector(document).OwnedByView(view.Id).WhereElementIsNotElementType():
        if len(contents) >= preview.MAX_DETAILS:
            raise ValueError("The drafting view exceeds the permitted detail count.")
        item = {"id": key(element.Id), "uid": str(element.UniqueId)}
        if isinstance(element, DB.TextNote):
            item.update(kind="note", text=normalized_text(element.Text),
                        type_id=key(element.GetTypeId()),
                        bounds=[round(value, 8) for value in note_bounds(view, element)])
        elif isinstance(element, DB.DetailCurve):
            ends = sorted(tuple(round(value, 8) for value in xy) for xy in line_ends(view, element))
            item.update(kind="line", ends=[list(xy) for xy in ends])
        else:
            raise ValueError("The legend view owns an unexpected element.")
        contents.append(item)
    return {"project": project, "view_id": key(view.Id), "view_uid": str(view.UniqueId),
            "name": element_name(view), "scale": int(view.Scale),
            "drafting_type_id": key(view.GetTypeId()),
            "contents": sorted(contents, key=lambda item: int(item["id"]))}


def check_budget(clock):
    if clock.ElapsedMilliseconds >= 30000:
        raise ValueError("Drafting exceeded its time budget. Use a smaller legend scope.")


def create_contents(document, view, plan):
    clock = Stopwatch.StartNew()
    items = plan["types"]
    options = DB.TextNoteOptions(identifier(plan["request"]["text_type_id"]))
    notes = []
    for item in items:
        check_budget(clock)
        note = DB.TextNote.Create(document, view.Id, point(view, (0, 0)), preview.note_text(item), options)
        if note is None:
            raise ValueError("Revit could not create a layer table.")
        notes.append(note)
    document.Regenerate()
    bounds = [note_bounds(view, note) for note in notes]
    sizes = [preview.section_size(item) for item in items]
    table_x = max(size[0] for size in sizes) + SKETCH_GAP_FT
    layout = [
        {"height_ft": max(box[3] - box[1], size[1] + preview.MEMBRANE_MARK_FT), "gap_ft": ROW_GAP_FT}
        for box, size in zip(bounds, sizes)
    ]
    tops = layout_rows(0.0, layout)
    drawn, bands = [], []
    for item, note, box, top in zip(items, notes, bounds, tops):
        check_budget(clock)
        move = view.RightDirection.Multiply(table_x - box[0]).Add(view.UpDirection.Multiply(top - box[3]))
        DB.ElementTransformUtils.MoveElement(document, note.Id, move)
        segments = preview.section_lines(item, top - preview.MEMBRANE_MARK_FT)
        for segment in segments:
            check_budget(clock)
            a, b = [point(view, xy) for xy in segment]
            if a.DistanceTo(b) <= document.Application.ShortCurveTolerance:
                raise ValueError("A section line is below Revit's short-curve tolerance.")
            curve = document.Create.NewDetailCurve(view, DB.Line.CreateBound(a, b))
            if curve is None:
                raise ValueError("Revit could not create a section line.")
            drawn.append((curve, segment))
        bands.append((note, segments))
    document.Regenerate()
    intervals = []
    for note, segments in bands:
        x0, y0, _, y1 = note_bounds(view, note)
        sketch_xmax = max(xy[0] for segment in segments for xy in segment)
        if x0 < sketch_xmax + SKETCH_GAP_FT - COORD_TOLERANCE_FT:
            raise ValueError("A layer table overlaps or crowds its section sketch.")
        sketch_ys = [xy[1] for segment in segments for xy in segment]
        intervals.append((min(y0, min(sketch_ys)), max(y1, max(sketch_ys))))
    if find_overlaps(intervals):
        raise ValueError("The generated legend rows overlap.")
    for element, expected in drawn:
        if not same_segment(line_ends(view, element), expected):
            raise ValueError("Revit did not retain the reviewed section geometry.")
    check_budget(clock)
    return {key(element.Id) for element in notes + [curve for curve, _ in drawn]}


def abort_transaction(txn, state=None, message=None):
    try:
        if state is None:
            state = txn.GetStatus()
        if state == DB.TransactionStatus.Started:
            state = txn.RollBack()
        if state == DB.TransactionStatus.RolledBack:
            return {"status": "stopped", "message": message or "Revit rolled back the drafting view."}
    except Exception:
        return {"status": "transaction_unresolved", "message": "Could not verify rollback of the drafting view."}
    return {"status": "transaction_unresolved", "message": "Check the Revit transaction state before continuing."}


def apply(document, approved, shown):
    if document is None or document.IsReadOnly:
        raise ValueError("Open the writable project used for the preview.")
    if shown.get("blocked") is not False or any(warning.get("blocking") for warning in shown.get("warnings", [])):
        raise ValueError("The reviewed legend preview contains blocking warnings.")
    if approved.get("recipe") != "compound_type_legend" or not approved.get("target"):
        raise ValueError("Supply the approved drafting view plan with its selected types and name.")
    fresh = preview.compute(read_snapshot(document, approved["request"]))
    preview_keys = ("kind", "keyColumn", "columns", "before", "after", "ownership_ids", "compound_legend_basis")
    if (fresh["plan"] != approved or fresh["preview"]["blocked"]
            or any(shown.get(field) != fresh["preview"][field] for field in preview_keys)):
        raise ValueError("The project, target or layer data changed after preview. Read and preview again.")
    count = sum(1 + len(preview.section_lines(item)) for item in approved["types"])
    if count > preview.MAX_DETAILS:
        raise ValueError("The legend would exceed 2,000 view-owned elements. Use a smaller batch.")

    txn = DB.Transaction(document, "Create compound wall and floor legend")
    if txn.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the drafting transaction."}
    try:
        view = DB.ViewDrafting.Create(document, identifier(approved["request"]["drafting_type_id"]))
        if view is None or key(view.ViewTemplateId) != "-1":
            raise ValueError("Choose a drafting view type that does not apply a view template.")
        view.Name = approved["request"]["view_name"]
        view.Scale = preview.VIEW_SCALE
        check_view(view)
        created = create_contents(document, view, approved)
        result = inspect_view(document, view)
        ids = preview.verify_contents(approved, result)
        if ids != created | {key(view.Id)}:
            raise ValueError("The drafting view contains unexpected owned elements.")
        receipt = preview.canonical(result)
        if len(receipt) > preview.MAX_BYTES:
            raise ValueError("The legend verification result exceeds 1 MiB. Select a smaller scope.")
        state = txn.Commit()
        if state != DB.TransactionStatus.Committed:
            return abort_transaction(txn, state)
        return {"status": "ok", "legend_json": receipt}
    except Exception as error:
        # After regeneration fails, query only the transaction status.
        return abort_transaction(txn, message=error.args[0] if isinstance(error, ValueError) else None)


def main(document, args):
    if args.get("mode") == "apply":
        return apply(document, args["plan"], args["preview"])
    if args.get("mode") == "verify":
        view = document.GetElement(identifier(args["view_id"]))
        if view is None or str(view.UniqueId) != args["view_uid"]:
            raise ValueError("The kept drafting view is unavailable.")
        return {"status": "ok", "legend_json": preview.canonical(inspect_view(document, view))}
    raise ValueError("Choose apply or verify.")


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": error.args[0]}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not create or verify the compound legend.",
                  "error_type": type(error).__name__}
