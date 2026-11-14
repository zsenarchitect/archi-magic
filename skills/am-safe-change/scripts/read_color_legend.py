# -*- coding: utf-8 -*-
"""Read the native drafting resources and any existing color legend candidate."""

from itertools import product

from pyrevit.api import DB

from duplicate_views import bounded, can_edit, identifier, key, owner_state, viewports
from read_view_filter_graphics import color, graphics, native_number, pattern_definition


def name_of(item):
    return str(DB.Element.Name.GetValue(item))


def identity(item):
    return {"id": key(item.Id), "uid": str(item.UniqueId), "name": name_of(item),
            "version": str(item.VersionGuid)}


def point(value):
    return [native_number(value.X), native_number(value.Y), native_number(value.Z)]


def frame(view):
    return {"origin": point(view.Origin), "right": point(view.RightDirection), "up": point(view.UpDirection)}


def note_bounds(view, note):
    box = note.get_BoundingBox(view)
    if box is None:
        return None
    points = []
    for coords in product((box.Min.X, box.Max.X), (box.Min.Y, box.Max.Y), (box.Min.Z, box.Max.Z)):
        offset = box.Transform.OfPoint(DB.XYZ(*coords)).Subtract(view.Origin)
        points.append((native_number(offset.DotProduct(view.RightDirection)),
                       native_number(offset.DotProduct(view.UpDirection))))
    xs, ys = zip(*points)
    return [min(xs), min(ys), max(xs), max(ys)]


def plain_note(note):
    formatted = note.GetFormattedText()
    try:
        text = formatted.GetPlainText()
        span = formatted.AsTextRange()
        flags = ("GetBoldStatus", "GetItalicStatus", "GetUnderlineStatus", "GetAllCapsStatus",
                 "GetSuperscriptStatus", "GetSubscriptStatus")
        return (text == note.Text and bool(text) and not any(item in text for item in ("\r", "\n", "\v", "\t"))
                and all(str(getattr(formatted, method)()) == "None" for method in flags)
                and str(formatted.GetListType(span)) == "None" and formatted.GetIndentLevel(span) == 0
                and str(note.HorizontalAlignment) == "Left")
    finally:
        formatted.Dispose()


def boundary_lines(region, clock):
    loops = []
    for loop in bounded(region.GetBoundaries(), 8, "legend region boundary", clock):
        lines = []
        for curve in bounded(loop, 32, "legend region edge", clock):
            if not isinstance(curve, DB.Line) or not curve.IsBound:
                return None
            lines.append(sorted([point(curve.GetEndPoint(0)), point(curve.GetEndPoint(1))]))
        loops.append(sorted(lines))
    return sorted(loops)


def read_contents(document, view, clock):
    solid = key(DB.LinePatternElement.GetSolidPatternId())
    collector = DB.FilteredElementCollector(document)
    contents = []
    try:
        owned = collector.OwnedByView(view.Id).WhereElementIsNotElementType()
        for item in bounded(owned, 128, "legend detail", clock):
            row = {"id": key(item.Id), "uid": str(item.UniqueId), "owner_id": key(item.OwnerViewId),
                   "type_id": key(item.GetTypeId()), "native_kind": str(item.GetType().Name)}
            if isinstance(item, (DB.FilledRegion, DB.TextNote)):
                row["visible"] = bool(item.Category is not None and not item.IsHidden(view)
                                      and not view.GetCategoryHidden(item.Category.Id))
            if isinstance(item, DB.FilledRegion):
                settings = view.GetElementOverrides(item.Id)
                try:
                    row.update(kind="region", lines=boundary_lines(item, clock),
                               graphics=graphics(settings, solid))
                finally:
                    settings.Dispose()
            elif isinstance(item, DB.TextNote):
                row.update(kind="note", text=str(item.Text), position=point(item.Coord),
                           leaders=len(list(bounded(item.GetLeaders(), 16, "legend leader", clock))),
                           plain=plain_note(item), bounds_ft=note_bounds(view, item))
            else:
                row["kind"] = "other"
            contents.append(row)
    finally:
        collector.Dispose()
    return sorted(contents, key=lambda row: int(row["id"]))


def legend_resources(document, proposed_name, request, clock):
    if not isinstance(request, dict) or set(request) != {
            "drafting_type_id", "region_type_id", "text_type_id", "solid_pattern_id"}:
        raise ValueError("Supply exact drafting, filled-region, text and solid-pattern IDs.")
    drafting, region, text, solid = (document.GetElement(identifier(request[field])) for field in
                                     ("drafting_type_id", "region_type_id", "text_type_id", "solid_pattern_id"))
    if not isinstance(drafting, DB.ViewFamilyType) or drafting.ViewFamily != DB.ViewFamily.Drafting:
        raise ValueError("The requested legend view type is not a drafting view type.")
    if not isinstance(region, DB.FilledRegionType) or not isinstance(text, DB.TextNoteType):
        raise ValueError("Select existing filled-region and text-note types.")
    if not isinstance(solid, DB.FillPatternElement):
        raise ValueError("Select a native drafting solid fill pattern.")
    fill = pattern_definition(document, key(solid.Id), False, [512], clock)
    if not fill["solid"]:
        raise ValueError("Select a native drafting solid fill pattern.")
    text_size = text.get_Parameter(DB.BuiltInParameter.TEXT_SIZE)
    if text_size is None or not text_size.HasValue or native_number(text_size.AsDouble()) <= 0:
        raise ValueError("The requested text-note type has no verified positive text size.")
    resources = {
        "drafting": dict(identity(drafting), default_template_id=key(drafting.DefaultTemplateId)),
        "region": dict(identity(region), foreground_pattern_id=key(region.ForegroundPatternId),
                       background_pattern_id=key(region.BackgroundPatternId),
                       foreground_color=color(region.ForegroundPatternColor),
                       background_color=color(region.BackgroundPatternColor), masking=bool(region.IsMasking),
                       line_weight=int(region.LineWeight)),
        "text": dict(identity(text), text_size_ft=native_number(text_size.AsDouble())),
        "solid": dict(fill, id=key(solid.Id)),
    }

    collector = DB.FilteredElementCollector(document)
    try:
        matches = [view for view in bounded(collector.OfClass(DB.View), 10000, "project view", clock)
                   if name_of(view).upper() == proposed_name.upper()]
    finally:
        collector.Dispose()
    state, existing = "available", None
    if len(matches) > 1:
        state = "name_conflict"
    elif matches:
        view = matches[0]
        existing = identity(view)
        if (not isinstance(view, DB.ViewDrafting) or view.IsTemplate or name_of(view) != proposed_name
                or key(view.GetTypeId()) != key(drafting.Id)):
            state = "name_conflict"
        elif key(view.ViewTemplateId) != "-1" or view.IsTemporaryViewPropertiesModeEnabled():
            state = "existing_graphics_unresolved"
        else:
            placements = viewports(document, [key(view.Id)], clock)[key(view.Id)]
            if bool(placements) != (str(view.GetPlacementOnSheetStatus()) != "NotPlaced"):
                raise ValueError("An existing legend has an unverified sheet placement.")
            ownership = owner_state(document, view)
            existing.update(scale=int(view.Scale), template_id=key(view.ViewTemplateId),
                            placements=placements, ownership=ownership, writable=can_edit(ownership),
                            frame=frame(view), contents=read_contents(document, view, clock))
            state = "existing_requires_verification"
    elif resources["drafting"]["default_template_id"] != "-1":
        state = "default_template_requires_verification"
    return {"name": proposed_name, "state": state, "resources": resources, "existing": existing}
