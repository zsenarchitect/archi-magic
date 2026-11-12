"""Read or apply a reviewed batch of Revit sheet placements through am-safe-change."""

from collections import defaultdict
import hashlib
from itertools import product
import math
from time import monotonic

from pyrevit.api import DB

from create_sheets import document_identity, element_id, element_key, owned_title_block
from duplicate_views import can_edit, capture_changes, owner_state, rollback
import sheet_placement_preview as preview

TRANSACTION = "Place reviewed views on sheets"
MAX_SCAN = 20000
READ_SECONDS = 10


def bounded_scan(items, budget, limit, deadline):
    for item in items:
        budget[0] += 1
        if budget[0] > limit or monotonic() >= deadline:
            raise ValueError("The complete placement inventory exceeds its scan or time budget.")
        yield item


def point_ft(point):
    if not math.isfinite(point.Z) or abs(point.Z) > preview.TOLERANCE_FT:
        raise ValueError("A placement anchor is not on the sheet plane.")
    return preview.coordinates([float(point.X), float(point.Y)], 2)


def measured_rectangle(bounds):
    values = preview.coordinates(bounds, 4)
    # A visible rule line can have zero height or width. Keep its occupied locus.
    for axis in (0, 1):
        if values[axis] == values[axis + 2]:
            values[axis] -= preview.TOLERANCE_FT
            values[axis + 2] += preview.TOLERANCE_FT
    return preview.rectangle(values)


def outline_rect(outline, *, empty_label=False):
    if outline is None:
        raise ValueError("Revit did not return a viewport outline.")
    try:
        if outline.IsEmpty:
            if empty_label:
                return None
            raise ValueError("The viewport body has an empty outline.")
        lo, hi = outline.MinimumPoint, outline.MaximumPoint
        bounds = [float(lo.X), float(lo.Y), float(hi.X), float(hi.Y)]
        return measured_rectangle(bounds) if empty_label else preview.rectangle(bounds)
    finally:
        outline.Dispose()


def sheet_rect(sheet):
    outline = sheet.Outline
    if outline is None:
        raise ValueError("The sheet outline is unavailable.")
    try:
        return preview.rectangle([float(outline.Min.U), float(outline.Min.V),
                                  float(outline.Max.U), float(outline.Max.V)])
    finally:
        outline.Dispose()


def graphic_rect(graphic, sheet):
    box = graphic.get_BoundingBox(sheet)
    if box is None:
        raise ValueError("A sheet graphic has no measurable bounds.")
    try:
        lo, hi = box.Min, box.Max
        corners = [box.Transform.OfPoint(DB.XYZ(*corner)) for corner in product(
            (lo.X, hi.X), (lo.Y, hi.Y), (lo.Z, hi.Z))]
        x, y = [float(point.X) for point in corners], [float(point.Y) for point in corners]
        if not all(math.isfinite(value) for value in x + y):
            raise ValueError("A transformed sheet bound is not finite.")
        return measured_rectangle([min(x), min(y), max(x), max(y)])
    finally:
        box.Dispose()


def destination(document, sheet_id):
    sheet = document.GetElement(element_id(sheet_id))
    if not isinstance(sheet, DB.ViewSheet) or sheet.IsPlaceholder:
        raise ValueError("Select a real destination sheet.")
    return sheet


def paper_state(document, sheet, block, deadline):
    revision_ids = sheet.GetAllRevisionIds()
    if revision_ids.Count > preview.MAX_GRAPHICS:
        raise ValueError("The sheet revision inventory exceeds its limit.")
    revisions = []
    for revision_id in revision_ids:
        if monotonic() >= deadline:
            raise ValueError("The sheet revision inventory exceeded its time budget.")
        revision = document.GetElement(revision_id)
        if not isinstance(revision, DB.Revision):
            raise ValueError("A sheet revision is unavailable.")
        revisions.append([element_key(revision.Id), str(revision.UniqueId), str(revision.VersionGuid)])
    symbol = document.GetElement(block.GetTypeId())
    if not isinstance(symbol, DB.FamilySymbol):
        raise ValueError("The reviewed title block type is unavailable.")
    block_state = [str(block.VersionGuid), element_key(symbol.Id), str(symbol.UniqueId), str(symbol.VersionGuid)]
    digest = hashlib.sha256(preview.encode([block_state, revisions]).encode("ascii")).hexdigest()
    return {"id": element_key(sheet.Id), "uid": str(sheet.UniqueId), "number": sheet.SheetNumber,
            "outline_ft": sheet_rect(sheet), "title_block_id": element_key(block.Id),
            "title_block_uid": str(block.UniqueId), "revision_sha256": digest}


def source_view(document, view_id):
    view = document.GetElement(element_id(view_id))
    if not isinstance(view, DB.View) or view.IsTemplate or isinstance(view, DB.ViewSheet):
        raise ValueError("Select an existing graphical view, legend, or whole schedule.")
    if isinstance(view, DB.ViewSchedule):
        kind = "schedule"
        eligible = (not view.IsTitleblockRevisionSchedule and not view.IsInternalKeynoteSchedule
                    and view.GetSegmentCount() == 1 and not view.Definition.IsFilteredBySheet)
    else:
        kind = "legend" if view.ViewType == DB.ViewType.Legend else "view"
        eligible = True
    return view, {"id": view_id, "uid": str(view.UniqueId), "name": view.Name, "kind": kind,
                  "eligible": bool(eligible), "saved_position": view.ViewPositionId.Value != -1,
                  "placed_sheet_ids": []}


def placement_state(document, sheet, item):
    if isinstance(item, DB.Viewport):
        if element_key(item.SheetId) != element_key(sheet.Id):
            raise ValueError("A viewport is not on its expected sheet.")
        view = document.GetElement(item.ViewId)
        if not isinstance(view, DB.View):
            raise ValueError("A placed viewport source is unavailable.")
        kind = "legend" if view.ViewType == DB.ViewType.Legend else "view"
        shapes = [outline_rect(item.GetBoxOutline())]
        label = outline_rect(item.GetLabelOutline(), empty_label=True)
        if label is not None:
            shapes.append(label)
        point = item.GetBoxCenter()
        view_id = item.ViewId
    elif isinstance(item, DB.ScheduleSheetInstance):
        if (item.IsTitleblockRevisionSchedule or item.SegmentIndex != -1
                or element_key(item.OwnerViewId) != element_key(sheet.Id)):
            raise ValueError("The instance is not a whole schedule on its expected sheet.")
        kind = "schedule"
        point = item.Point
        shapes = [graphic_rect(item, sheet)]
        view_id = item.ScheduleId
    else:
        raise ValueError("Revit returned an unsupported sheet placement.")
    return {"id": element_key(item.Id), "uid": str(item.UniqueId), "view_id": element_key(view_id),
            "kind": kind, "point_ft": point_ft(point), "rectangles_ft": shapes}


def read_snapshot(document, request, *, excluded=frozenset(), placed_pairs=frozenset()):
    deadline = monotonic() + READ_SECONDS
    identity = document_identity(document)
    pairs = [pair for pair, _ in preview.request_rows(request)]
    views = {view_id: source_view(document, view_id) for view_id in sorted({pair[1] for pair in pairs}, key=int)}
    sheets = {sheet_id: destination(document, sheet_id) for sheet_id in sorted(request["sheets"], key=int)}
    locations = defaultdict(set)
    budget = [0]
    collector = DB.FilteredElementCollector(document).OfClass(DB.Viewport)
    try:
        for item in bounded_scan(collector, budget, MAX_SCAN, deadline):
            key = element_key(item.ViewId)
            if key in views and item.SheetId.Value != -1 and element_key(item.Id) not in excluded:
                locations[key].add(preview.element_id(element_key(item.SheetId)))
    finally:
        collector.Dispose()
    for view_id, (view, state) in views.items():
        if state["kind"] == "schedule" and state["eligible"]:
            for identifier in bounded_scan(view.GetScheduleInstances(-1), budget, MAX_SCAN, deadline):
                if element_key(identifier) in excluded:
                    continue
                item = document.GetElement(identifier)
                if (not isinstance(item, DB.ScheduleSheetInstance) or item.SegmentIndex != -1
                        or item.IsTitleblockRevisionSchedule or element_key(item.ScheduleId) != view_id):
                    raise ValueError("A whole-schedule placement could not be verified.")
                locations[view_id].add(preview.element_id(element_key(item.OwnerViewId)))
        state["placed_sheet_ids"] = sorted(locations[view_id], key=int)
    graphics = [0]
    sheet_rows = []
    for sheet in sheets.values():
        block = owned_title_block(document, sheet)
        row = paper_state(document, sheet, block, deadline)
        row.update(ownership_ok=can_edit(owner_state(document, sheet)), placements=[], obstacles=[])
        viewport_ids = set()
        for viewport_id in bounded_scan(sheet.GetAllViewports(), graphics, preview.MAX_GRAPHICS, deadline):
            identifier = element_key(viewport_id)
            if identifier in viewport_ids:
                raise ValueError("The sheet repeated a viewport identity.")
            viewport_ids.add(identifier)
            if identifier not in excluded:
                viewport = document.GetElement(viewport_id)
                if not isinstance(viewport, DB.Viewport):
                    raise ValueError("A sheet viewport is unavailable.")
                row["placements"].append(placement_state(document, sheet, viewport))
        collector = DB.FilteredElementCollector(document).OwnedByView(sheet.Id).WhereElementIsNotElementType()
        try:
            other_items = (item for item in collector if element_key(item.Id) not in viewport_ids)
            for item in bounded_scan(other_items, graphics, preview.MAX_GRAPHICS, deadline):
                identifier = element_key(item.Id)
                if identifier in excluded or identifier == row["title_block_id"]:
                    continue
                if element_key(item.OwnerViewId) != row["id"]:
                    raise ValueError("A sheet-owned graphic changed ownership.")
                if isinstance(item, DB.Viewport):
                    raise ValueError("The owned viewport list differs from the sheet viewport list.")
                if (isinstance(item, DB.ScheduleSheetInstance)
                        and not item.IsTitleblockRevisionSchedule and item.SegmentIndex == -1):
                    row["placements"].append(placement_state(document, sheet, item))
                elif str(item.GetType().Name) != "ExtentElem":
                    row["obstacles"].append({"id": identifier, "rectangles_ft": [graphic_rect(item, sheet)]})
        finally:
            collector.Dispose()
        for name in ("placements", "obstacles"):
            row[name].sort(key=lambda item: int(item["id"]))
        sheet_rows.append(row)

    eligible = []
    for sheet_id, view_id in pairs:
        _, state = views[view_id]
        if state["kind"] != "schedule" and state["eligible"] and (
            (sheet_id, view_id) in placed_pairs
            or DB.Viewport.CanAddViewToSheet(document, sheets[sheet_id].Id, views[view_id][0].Id)
        ):
            eligible.append([sheet_id, view_id])
    if monotonic() >= deadline:
        raise ValueError("The complete sheet snapshot exceeded its time budget.")
    return {"kind": "sheet-placement-snapshot", "complete": True, "document": identity, "request": request,
            "views": [state for _, state in views.values()], "sheets": sheet_rows, "viewport_eligible_pairs": eligible}


def measured_batch(document, steps, ids):
    measured = []
    for step, identifier in zip(steps, ids):
        sheet = destination(document, step["sheet_id"])
        instance = document.GetElement(element_id(identifier))
        record = placement_state(document, sheet, instance)
        view = document.GetElement(element_id(record["view_id"]))
        record.update(sheet_id=element_key(sheet.Id), sheet_uid=str(sheet.UniqueId), view_uid=str(view.UniqueId))
        measured.append(record)
    return measured


def baseline_without_created(document, plan, ids):
    # A newly placed ordinary view fails CanAdd. Restore only its original eligible pair
    # after verifying the exact additions, and reread every other baseline field.
    pairs = {(item["sheet_id"], item["view_id"]) for item in plan["steps"] if item["kind"] != "schedule"}
    return read_snapshot(document, plan["request"], excluded=set(ids), placed_pairs=pairs)


def apply(document, plan):
    if document is None or document.IsReadOnly:
        raise ValueError("Open the writable project used for the sheet placement preview.")
    baseline = read_snapshot(document, plan["request"])
    if preview.compute(baseline) != plan or plan["blocked"]:
        raise ValueError("The approved placement plan differs from the current project or request.")
    steps = plan["steps"]
    if not steps:
        return {"status": "no_changes", "message": "The reviewed placements already exist."}
    application = document.Application
    events = []

    def record_change(_sender, event):
        try:
            if (events or not event.GetDocument().Equals(document)
                    or list(event.GetTransactionNames()) != [TRANSACTION]):
                events.append(None)
                return
            added = event.GetAddedElementIds()
            modified, deleted = event.GetModifiedElementIds(), event.GetDeletedElementIds()
            if added.Count != len(steps) or modified.Count or deleted.Count:
                events.append(None)
                return
            events.append(({element_key(item) for item in added}, set(), set()))
        except Exception:
            events.append(None)

    group = DB.TransactionGroup(document, TRANSACTION)
    transaction = DB.Transaction(document, TRANSACTION)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the placement group."}
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the placement transaction.")
        application.DocumentChanged += record_change
        try:
            ids, centers = [], []
            for step in steps:
                sheet = destination(document, step["sheet_id"])
                if not can_edit(owner_state(document, sheet)):
                    raise ValueError("Destination sheet ownership changed before placement.")
                view_id = element_id(step["view_id"])
                point = DB.XYZ(*step["point_ft"], 0)
                if step["kind"] == "schedule":
                    item = DB.ScheduleSheetInstance.Create(document, sheet.Id, view_id, point)
                else:
                    if not DB.Viewport.CanAddViewToSheet(document, sheet.Id, view_id):
                        raise ValueError("Revit declined the reviewed viewport placement.")
                    item = DB.Viewport.Create(document, sheet.Id, view_id, point)
                    centers.append((item, point))
                if item is None:
                    raise ValueError("Revit did not create a reviewed placement.")
                ids.append(element_key(item.Id))
            document.Regenerate()
            for item, point in centers:
                item.SetBoxCenter(point)
            document.Regenerate()
            preview.verify_layout(plan, baseline, measured_batch(document, steps, ids))
            state = transaction.Commit()
        finally:
            application.DocumentChanged -= record_change
        if state != DB.TransactionStatus.Committed or capture_changes(events)["added"] != sorted(ids, key=int):
            raise ValueError("The committed placements contain unexpected or unverifiable changes.")

        current = baseline_without_created(document, plan, ids)
        measured = preview.verify_layout(plan, current, measured_batch(document, steps, ids))
        receipt = {"document": plan["document"], "basis_sha256": plan["basis_sha256"], "placements": measured}
        payload = preview.encode(receipt)
        if group.Assimilate() != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not finish the placement group.")
        return {"status": "ok", "receipt_json": payload}
    except Exception as error:
        if rollback(transaction, group):
            message = str(error) if isinstance(error, ValueError) else "Revit could not place or verify the reviewed views."
            return {"status": "stopped", "message": message}
        return {"status": "transaction_unresolved", "message": "Check the Revit transaction before continuing."}


def verify_kept(document, plan, receipt):
    ids = preview.receipt_ids(plan, receipt)
    current = baseline_without_created(document, plan, ids)
    measured = preview.verify_layout(plan, current, measured_batch(document, plan["steps"], ids))
    if not preview.same_placement_batch(receipt["placements"], measured, exact_ids=True):
        raise ValueError("The independently read kept placements differ from the kept receipt.")
    return {"status": "ok", "verification_json": preview.encode(
        {"document": plan["document"], "basis_sha256": plan["basis_sha256"], "placements": measured})}


def main(document, args):
    mode = args.get("mode", "read")
    if mode == "sheet-catalog":
        identity = document_identity(document)
        ids = preview.bounded(args["sheet_ids"], preview.MAX_ROWS)
        if not ids or len(set(ids)) != len(ids):
            raise ValueError("Choose distinct destination sheet IDs.")
        deadline = monotonic() + READ_SECONDS
        rows = []
        for identifier in ids:
            sheet = destination(document, identifier)
            rows.append(paper_state(document, sheet, owned_title_block(document, sheet), deadline))
        return {"status": "ok", "catalog_json": preview.encode({"document": identity, "sheets": rows})}
    if mode == "apply":
        return apply(document, args["plan"])
    if mode == "verify":
        return verify_kept(document, args["plan"], args["receipt"])
    if mode != "read":
        raise ValueError("Choose sheet-catalog, read, apply, or verify.")
    return {"status": "ok", "snapshot_json": preview.encode(read_snapshot(document, args["request"]))}


if "inputs" in globals():
    try:
        result = main(doc, inputs)
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not read or verify the sheet placement scope.",
                  "error_type": type(error).__name__}
