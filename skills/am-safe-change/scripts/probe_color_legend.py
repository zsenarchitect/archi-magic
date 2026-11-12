# -*- coding: utf-8 -*-
"""Create and verify color swatches in a disposable Revit legend, then roll back."""

import json

from System.Collections.Generic import List
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

from duplicate_views import bounded, capture_changes, identifier, key, rollback
from read_color_legend import name_of, point, read_contents
from read_color_parameter_values import main as read_inventory
from view_color_preview import build, close_points, note_fits_row

TRANSACTION_NAME = "Test parameter color drafting legend"


def world(view, x, y):
    return view.Origin.Add(view.RightDirection.Multiply(x)).Add(view.UpDirection.Multiply(y))


def corners(view, bounds):
    left, bottom, right, top = bounds
    return [world(view, x, y) for x, y in ((left, bottom), (right, bottom), (right, top), (left, top))]


def validate_view(view, proposal):
    if (not isinstance(view, DB.ViewDrafting) or view.IsTemplate or int(view.Scale) != 1
            or key(view.ViewTemplateId) != "-1" or view.IsTemporaryViewPropertiesModeEnabled()
            or name_of(view) != proposal["legend_name"]
            or key(view.GetTypeId()) != proposal["legend_preflight"]["resources"]["drafting"]["id"]):
        raise ValueError("Revit did not create the reviewed untemplated drafting view.")


def create_rows(document, view, proposal):
    resources = proposal["legend_preflight"]["resources"]
    region_type = identifier(resources["region"]["id"])
    text_type = identifier(resources["text"]["id"])
    solid = identifier(resources["solid"]["id"])
    created = {}
    for row in proposal["legend_rows"]:
        vertices = corners(view, row["swatch_bounds_ft"])
        loop = DB.CurveLoop()
        try:
            for start, end in zip(vertices, vertices[1:] + vertices[:1]):
                loop.Append(DB.Line.CreateBound(start, end))
            boundaries = List[DB.CurveLoop]()
            boundaries.Add(loop)
            region = DB.FilledRegion.Create(document, region_type, view.Id, boundaries)
        finally:
            loop.Dispose()
        settings = DB.OverrideGraphicSettings()
        try:
            settings.SetSurfaceForegroundPatternId(solid)
            settings.SetSurfaceForegroundPatternColor(DB.Color(*row["rgb"]))
            settings.SetSurfaceForegroundPatternVisible(True)
            settings.SetSurfaceBackgroundPatternVisible(False)
            view.SetElementOverrides(region.Id, settings)
        finally:
            settings.Dispose()
        note = DB.TextNote.Create(document, view.Id, world(view, *row["label_origin_ft"]), row["label"], text_type)
        created[row["value_key"]] = {"region": key(region.Id), "note": key(note.Id)}
    return created


def verify_rows(document, view, proposal, created):
    validate_view(view, proposal)
    resources = proposal["legend_preflight"]["resources"]
    contents = {row["id"]: row for row in read_contents(document, view, Stopwatch.StartNew())}
    if set(contents) != {item for pair in created.values() for item in pair.values()}:
        raise ValueError("Revit added unreviewed legend contents or omitted a swatch or note.")
    verified = []
    for row in proposal["legend_rows"]:
        pair = created[row["value_key"]]
        region, note = contents[pair["region"]], contents[pair["note"]]
        vertices = [point(vertex) for vertex in corners(view, row["swatch_bounds_ft"])]
        edges = sorted(sorted([start, end]) for start, end in zip(vertices, vertices[1:] + vertices[:1]))
        graphics = region["graphics"]
        if (region["owner_id"] != key(view.Id) or region["kind"] != "region"
                or region["type_id"] != resources["region"]["id"] or not region["visible"] or not region["lines"]
                or not close_points(region["lines"], [edges])
                or graphics["SurfaceForegroundPatternId"] != {"kind": "element", "id": resources["solid"]["id"]}
                or graphics["SurfaceForegroundPatternColor"] != row["rgb"]
                or not graphics["IsSurfaceForegroundPatternVisible"] or graphics["IsSurfaceBackgroundPatternVisible"]):
            raise ValueError("A native swatch differs from the reviewed color or geometry.")
        if (note["owner_id"] != key(view.Id) or note["kind"] != "note"
                or note["type_id"] != resources["text"]["id"] or note["text"] != row["label"] or note["leaders"]
                or not note["visible"] or not note["plain"]
                or not close_points(note["position"], point(world(view, *row["label_origin_ft"])))):
            raise ValueError("A native legend note differs from the reviewed text or placement.")
        if not note_fits_row(note["bounds_ft"], row, resources["text"]["text_size_ft"]):
            raise ValueError("A native note overlaps the swatch or extends outside its legend row.")
        verified.append({"value_key": row["value_key"], "rgb": row["rgb"],
                         "region_id": region["id"], "region_uid": region["uid"],
                         "note_id": note["id"], "note_uid": note["uid"]})
    return verified


def read_snapshot(document, request):
    return json.loads(read_inventory(document, request)["inventory_json"])


def verify_unchanged(document, request, baseline, proposal):
    from color_filter_trial import fixture_view, require_normal_display

    model_view, order = fixture_view(document, baseline, proposal)
    if set(order) != {row["filter_id"] for row in proposal["legend_rows"]}:
        raise ValueError("The applied filters changed during repeat verification.")
    reuse = proposal["legend_reuse"]
    legend = document.GetElement(identifier(reuse["view_id"]))
    if not isinstance(legend, DB.ViewDrafting) or str(legend.UniqueId) != reuse["view_uid"]:
        raise ValueError("The existing legend identity changed during repeat verification.")
    require_normal_display(legend)
    if read_snapshot(document, request) != baseline:
        raise ValueError("The color or legend inventory changed during repeat verification.")
    current_order = [key(item) for item in bounded(
        model_view.GetOrderedFilters(), 100, "repeat fixture filter", Stopwatch.StartNew())]
    if current_order != order:
        raise ValueError("The filter order changed during repeat verification.")
    return {"status": "color_fixture_unchanged", "receipt": {
        "basis_sha256": proposal["basis_sha256"], "ordered_filter_ids": order, "legend_reuse": reuse,
    }}


def probe(document, args):
    if args.get("disposable_fixture") is not True or document.IsWorkshared:
        raise ValueError("Use an explicitly confirmed standalone disposable fixture.")
    exercise_filters = args.get("exercise_filters", False)
    if type(exercise_filters) is not bool:
        raise ValueError("The filter trial flag must be true or false.")
    request = args["request"]
    baseline = read_snapshot(document, request)
    proposal, display = build(baseline)
    if (proposal != args["proposal"] or display != args["preview"] or proposal["blocked"]
            or not proposal["legend_preflight"]):
        raise ValueError("The reviewed proposal, preview or drafting resources do not match the live fixture.")
    if exercise_filters and proposal["no_change_candidate"]:
        return verify_unchanged(document, request, baseline, proposal)
    if proposal["legend_preflight"]["state"] != "available":
        raise ValueError("The existing legend requires an exact, unchanged combined fixture.")

    model_view, order, filter_trial = None, [], None
    if exercise_filters:
        from color_filter_trial import apply_filters, fixture_view, verify_filters

        model_view, order = fixture_view(document, baseline, proposal)
    events, foreign = [], []
    application = document.Application

    def record(_sender, event):
        try:
            if not event.GetDocument().Equals(document):
                foreign[:] = [True]
            if events or foreign or list(event.GetTransactionNames()) != [TRANSACTION_NAME]:
                events[:] = [None]
                return
            added, modified, deleted = event.GetAddedElementIds(), event.GetModifiedElementIds(), event.GetDeletedElementIds()
            limit = 1 + (3 if exercise_filters else 2) * len(proposal["legend_rows"])
            if added.Count > limit or modified.Count > int(exercise_filters) or deleted.Count:
                events.append(None)
            else:
                events.append(({key(item) for item in added}, {key(item) for item in modified}, set()))
        except Exception:
            events[:] = [None]

    group = DB.TransactionGroup(document, TRANSACTION_NAME)
    transaction = DB.Transaction(document, TRANSACTION_NAME)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the legend rollback group."}
    failure, receipt = None, None
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the legend trial transaction.")
        application.DocumentChanged += record
        try:
            if exercise_filters:
                filter_trial = apply_filters(document, model_view, order, baseline, proposal)
            view = DB.ViewDrafting.Create(document, identifier(proposal["legend_preflight"]["resources"]["drafting"]["id"]))
            view.Name, view.Scale = proposal["legend_name"], 1
            validate_view(view, proposal)
            pairs = create_rows(document, view, proposal)
            document.Regenerate()
            state = transaction.Commit()
        finally:
            application.DocumentChanged -= record
        if state != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not finish the legend trial transaction.")
        changes = capture_changes(events)
        expected = {key(view.Id)} | {item for pair in pairs.values() for item in pair.values()}
        expected_modified = set()
        if filter_trial is not None:
            expected.update(filter_trial["added"])
            if filter_trial["changed"]:
                expected_modified.add(key(model_view.Id))
        if set(changes["added"]) != expected or set(changes["modified"]) != expected_modified:
            raise ValueError("Revit reported changes outside the reviewed legend or filter scope.")
        receipt = {"basis_sha256": proposal["basis_sha256"], "legend_id": key(view.Id),
                   "legend_uid": str(view.UniqueId), "rows": verify_rows(document, view, proposal, pairs),
                   "changes": changes}
        if filter_trial is not None:
            receipt["filters"] = verify_filters(document, model_view, baseline, filter_trial)
            fresh = read_snapshot(document, request)
            for field in ("rows", "types", "link_candidate_ids", "native_rules"):
                if fresh[field] != baseline[field]:
                    raise ValueError("The source values or native rule matches changed during the color trial.")
    except Exception as error:
        failure = str(error)
    if not rollback(transaction, group) or foreign:
        return {"status": "transaction_unresolved", "message": "Revit did not confirm a clean legend rollback."}
    try:
        if read_snapshot(document, request) != baseline:
            raise ValueError("The fresh color and legend inventory differs after rollback.")
        if receipt and any(document.GetElement(identifier(item)) is not None for item in receipt["changes"]["added"]):
            raise ValueError("Revit still resolves a trial legend element after rollback.")
    except Exception as error:
        return {"status": "transaction_unresolved", "message": str(error)}
    if failure is not None:
        return {"status": "stopped_and_rolled_back", "message": failure}
    return {"status": "color_trial_rolled_back" if exercise_filters else "legend_trial_rolled_back",
            "receipt": receipt}


if "inputs" in globals():
    try:
        result = probe(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception:
        result = {"status": "transaction_unresolved", "message": "Revit could not verify the legend trial."}
