"""Read candidate identities and references for a Revit model cleanup review."""

import hashlib
import json
import re

from pyrevit.api import DB
from System.Collections.Generic import HashSet
from System.Diagnostics import Stopwatch

MAX_SCAN = 5000
MAX_ROWS = 500
MAX_SECONDS = 15
MAX_EVIDENCE = 5000
MAX_GRAPHIC_VIEWS = 100
MAX_SELECTION = 20


class ScanLimit(Exception):
    pass


class References(dict):
    def __init__(self):
        dict.__init__(self)
        self.count = 0


def element_id(identifier):
    return str(identifier.Value)


def project_identity(document, active_view):
    if not document.PathName or active_view is None:
        return None
    return {"project_uid": str(document.ProjectInformation.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest(),
            "active_view_id": element_id(active_view.Id), "active_view_uid": str(active_view.UniqueId)}


def check_time(clock):
    if clock.Elapsed.TotalSeconds >= MAX_SECONDS:
        raise ScanLimit("time_limit")


def bounded(items, clock):
    for index, item in enumerate(items):
        check_time(clock)
        if index >= MAX_SCAN:
            raise ScanLimit("scan_limit")
        yield item
    check_time(clock)


def collect(document, clock, cls=None, category=None):
    check_time(clock)
    collector = DB.FilteredElementCollector(document)
    try:
        if cls is not None:
            collector.OfClass(cls)
        if category is not None:
            collector.OfCategory(category).WhereElementIsNotElementType()
        return list(bounded(collector, clock))
    finally:
        collector.Dispose()


def add_reference(references, target, source, kind):
    target_id = element_id(target)
    if target_id == "-1":
        return
    if references.count >= MAX_EVIDENCE:
        raise ScanLimit("reference_limit")
    references.setdefault(target_id, []).append({"source_id": element_id(source), "kind": kind})
    references.count += 1


def read_placement_references(document, clock, references):
    for viewport in collect(document, clock, cls=DB.Viewport):
        add_reference(references, viewport.ViewId, viewport.SheetId, "viewport")
    for schedule in collect(document, clock, cls=DB.ScheduleSheetInstance):
        add_reference(references, schedule.ScheduleId, schedule.OwnerViewId, "schedule")
    for panel in collect(document, clock, cls=DB.Electrical.PanelScheduleSheetInstance):
        check_time(clock)
        add_reference(references, panel.ScheduleId, panel.OwnerViewId, "panel_schedule")


def read_scope_box(owner, parameter, references):
    value = owner.get_Parameter(parameter)
    if value is not None:
        add_reference(references, value.AsElementId(), owner.Id, "scope_box")


def read_view_references(document, views, clock, references):
    for view_type in collect(document, clock, cls=DB.ViewFamilyType):
        add_reference(references, view_type.DefaultTemplateId, view_type.Id, "default_template")
    for datum in collect(document, clock, cls=DB.DatumPlane):
        check_time(clock)
        read_scope_box(datum, DB.BuiltInParameter.DATUM_VOLUME_OF_INTEREST, references)
    for view in bounded(views, clock):
        add_reference(references, view.ViewTemplateId, view.Id, "view_template")
        read_scope_box(view, DB.BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP, references)
        if view.AreGraphicsOverridesAllowed():
            for identifier in bounded(view.GetFilters(), clock):
                add_reference(references, identifier, view.Id, "attached_filter")
        if view.IsTemplate:
            continue
        primary = view.GetPrimaryViewId()
        if element_id(primary) != "-1":
            add_reference(references, primary, view.Id, "dependent_view")
            add_reference(references, view.Id, primary, "primary_view")
        for getter in (view.GetReferenceCallouts, view.GetReferenceSections, view.GetReferenceElevations):
            check_time(clock)
            for marker in bounded(getter(), clock):
                target = DB.ReferenceableViewUtils.GetReferencedViewId(document, marker)
                if element_id(target) == "-1" or document.GetElement(target) is None:
                    raise ValueError("unresolved_view_reference")
                add_reference(references, target, view.Id, "reference_view")


def view_element_ids(document, view, clock, owned=False, cls=None):
    check_time(clock)
    collector = (DB.FilteredElementCollector(document) if owned
                 else DB.FilteredElementCollector(document, view.Id))
    try:
        if owned:
            collector.OwnedByView(view.Id)
        if cls is not None:
            collector.OfClass(cls)
        collector.WhereElementIsNotElementType()
        for element in bounded(collector, clock):
            yield element_id(element.Id)
    finally:
        collector.Dispose()


def first_other_element(ids, view_id):
    try:
        return next((identifier for identifier in ids if identifier != view_id), None)
    finally:
        ids.close()


def read_graphic_evidence(document, views, rows, references, clock):
    """Record positive use evidence and the limits of negative observations."""
    imports = {row["id"]: row for row in rows if row["kind"] == "cad" and not row["linked"]}
    drafting = {row["id"]: row for row in rows if row.get("view_type") == "DraftingView"}
    if not imports and not drafting:
        return None, None
    for row in imports.values():
        row["potentially_visible_in"] = []
        row["owner_graphics_checked"] = False
    for row in drafting.values():
        row["content"] = {"status": "not_scanned"}
    owner_views = {}
    for row in imports.values():
        owner_views.setdefault(row["owner_view_id"], []).append(row)
    placed = {key for key, edges in references.items()
              if any(edge["kind"] == "viewport" for edge in edges)}
    coverage = {"visited_views": 0, "checked_graphic_views": 0, "unsupported_graphic_views": 0}
    hits = 0
    unsupported_draft = False
    try:
        check_time(clock)
        view_order = sorted((view for view in views if not view.IsTemplate), key=lambda view: (
            element_id(view.Id) not in owner_views, element_id(view.Id) not in placed,
            element_id(view.Id) not in drafting, int(view.Id.Value)))
        for view in view_order:
            view_id = element_id(view.Id)
            draft = drafting.get(view_id)
            if not imports and draft is None:
                continue
            check_time(clock)
            if coverage["visited_views"] >= MAX_GRAPHIC_VIEWS:
                raise ScanLimit("graphic_view_limit")
            coverage["visited_views"] += 1
            can_iterate = DB.FilteredElementCollector.IsViewValidForElementIteration(document, view.Id)
            if draft is not None:
                content = {"status": "incomplete"}
                draft["content"] = content
                content["owned_example_id"] = first_other_element(
                    view_element_ids(document, view, clock, owned=True), view_id)
                if can_iterate:
                    content["graphic_example_id"] = first_other_element(
                        view_element_ids(document, view, clock), view_id)
                    content["status"] = ("observed" if content["owned_example_id"]
                                         or content["graphic_example_id"] else "none_observed")
                elif content["owned_example_id"]:
                    content["status"] = "observed"
            if not can_iterate:
                coverage["unsupported_graphic_views"] += 1
                unsupported_draft = unsupported_draft or draft is not None
                continue
            if imports:
                for identifier in view_element_ids(document, view, clock, cls=DB.ImportInstance):
                    if identifier in imports:
                        if hits >= MAX_EVIDENCE:
                            raise ScanLimit("graphic_evidence_limit")
                        imports[identifier]["potentially_visible_in"].append(view_id)
                        hits += 1
                for row in owner_views.get(view_id, []):
                    row["owner_graphics_checked"] = True
            coverage["checked_graphic_views"] += 1
    except ScanLimit as error:
        return coverage, str(error)
    except Exception as error:
        return coverage, type(error).__name__
    return coverage, "unsupported_drafting_view" if unsupported_draft else None


def selection_ids(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_SELECTION
            or any(not isinstance(value, str) or not re.match(r"^[1-9][0-9]{0,18}\Z", value)
                   or int(value) > 2**63 - 1 for value in values) or len(set(values)) != len(values)):
        raise ValueError("Select one to 20 unique positive decimal-string element IDs.")
    return sorted(values, key=int)


def protection_digest(candidates, references, clock):
    identities = sorted((element_id(item.Id), kind, str(item.UniqueId), str(item.VersionGuid), bool(item.Pinned))
                        for kind, item in bounded(candidates, clock))
    edges = sorted((target, edge["kind"], edge["source_id"])
                   for target, items in references.items() for edge in items)
    check_time(clock)
    raw = json.dumps([identities, edges], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def read_inventory(document, selected_ids=None):
    if document is None or document.IsFamilyDocument:
        return {"kind": "revit-cleanup-inventory", "status": "stopped", "reason": "no_project_document"}
    if str(document.Application.VersionNumber) != "2026":
        return {"kind": "revit-cleanup-inventory", "status": "stopped", "reason": "revit_2026_required"}
    if selected_ids is not None:
        selected_ids = selection_ids(selected_ids)
    clock = Stopwatch.StartNew()
    rows, incomplete = [], []
    references = References()
    views = []
    candidates = []
    active_view = document.ActiveView
    try:
        identity = project_identity(document, active_view)
    except Exception as error:
        identity = None
        incomplete.append({"group": "document", "reason": type(error).__name__})
    stage = "inventory"
    try:
        views = collect(document, clock, cls=DB.View)
        candidates.extend(("view_template", view) for view in views if view.IsTemplate)
        for cls in (DB.ParameterFilterElement, DB.SelectionFilterElement):
            candidates.extend(("filter", item) for item in collect(document, clock, cls=cls))
        candidates.extend(("scope_box", item) for item in collect(
            document, clock, category=DB.BuiltInCategory.OST_VolumeOfInterest))
        imports = collect(document, clock, cls=DB.ImportInstance)
        candidates.extend(("cad", item) for item in imports)
        candidates.extend(("view", view) for view in views if not view.IsTemplate)

        stage = "references"
        if active_view is not None:
            add_reference(references, active_view.Id, active_view.Id, "active_view")
        read_placement_references(document, clock, references)
        read_view_references(document, views, clock, references)
        sheets = {element_id(view.Id) for view in bounded(views, clock) if str(view.ViewType) == "DrawingSheet"}
        for item in bounded(imports, clock):
            if element_id(item.OwnerViewId) in sheets:
                add_reference(references, item.Id, item.OwnerViewId, "sheet_owned_import")
    except ScanLimit as error:
        incomplete.append({"group": stage, "reason": str(error)})
    except Exception as error:
        incomplete.append({"group": stage, "reason": type(error).__name__})

    scope = None
    if selected_ids is not None:
        scope = {"selected_ids": selected_ids, "model_candidate_count": len(candidates), "protection_sha256": None}
        if not incomplete:
            try:
                scope["protection_sha256"] = protection_digest(candidates, references, clock)
            except ScanLimit as error:
                incomplete.append({"group": "protection", "reason": str(error)})
            except Exception as error:
                incomplete.append({"group": "protection", "reason": type(error).__name__})
        wanted = set(selected_ids)
        candidates = [(kind, item) for kind, item in candidates if element_id(item.Id) in wanted]

    unused = None
    try:
        categories = HashSet[DB.ElementId]()
        candidate_ids = set()
        for _, item in bounded(candidates, clock):
            candidate_ids.add(element_id(item.Id))
            category = item.Category
            categories.Add(category.Id if category is not None else DB.ElementId(DB.BuiltInCategory.INVALID))
        if categories.Count:
            check_time(clock)
            unused = set(element_id(identifier) for identifier in bounded(
                document.GetUnusedElements(categories), clock)) & candidate_ids
    except ScanLimit as error:
        incomplete.append({"group": "purge_unused", "reason": str(error)})
    except Exception as error:
        incomplete.append({"group": "purge_unused", "reason": type(error).__name__})

    evidence_count = 0
    try:
        for kind, item in bounded(candidates, clock):
            if len(rows) >= MAX_ROWS:
                raise ScanLimit("row_limit")
            identifier = element_id(item.Id)
            related = list(references.get(identifier, []))
            row = {"kind": kind, "id": identifier, "uid": str(item.UniqueId),
                   "version_guid": str(item.VersionGuid), "pinned": bool(item.Pinned),
                   "purge_unused": identifier in unused if unused is not None else None,
                   "references": related, "disposition": "report_only"}
            if kind == "cad":
                owner = element_id(item.OwnerViewId)
                row.update({"linked": bool(item.IsLinked), "view_specific": bool(item.ViewSpecific),
                            "owner_view_id": owner})
                related.extend(references.get(owner, []))
            if kind == "view":
                row["view_type"] = str(item.ViewType)
            row["dependent_ids"] = [element_id(dependent) for dependent in bounded(
                item.GetDependentElements(None), clock)]
            evidence_count += len(related) + len(row["dependent_ids"])
            if evidence_count > MAX_EVIDENCE:
                raise ScanLimit("evidence_limit")
            rows.append(row)
    except ScanLimit as error:
        incomplete.append({"group": "rows", "reason": str(error)})
    except Exception as error:
        incomplete.append({"group": "rows", "reason": type(error).__name__})
    coverage, reason = read_graphic_evidence(document, views, rows, references, clock)
    if reason is not None:
        incomplete.append({"group": "graphics", "reason": reason})
    for row in rows:
        row["dependent_ids"].sort(key=int)
        row["references"].sort(key=lambda edge: (edge["kind"], int(edge["source_id"])))
    rows.sort(key=lambda row: int(row["id"]))
    result = {"kind": "revit-cleanup-inventory", "status": "incomplete" if incomplete else "reported",
              "rows": rows, "incomplete": incomplete, "candidate_count": len(candidates),
              "graphic_coverage": coverage, "document": identity}
    if scope is not None:
        result["scope"] = scope
    return result


if "doc" in globals():
    request = globals().get("inputs")
    if request is None:
        request = {}
    try:
        if not isinstance(request, dict) or set(request) - {"selected_ids"}:
            raise ValueError("The only inventory input is selected_ids.")
        if "selected_ids" in request and request["selected_ids"] is None:
            raise ValueError("Supply a nonempty selected_ids list or omit the field.")
        result = read_inventory(doc, request.get("selected_ids"))  # noqa: F821
    except ValueError as error:
        result = {"kind": "revit-cleanup-inventory", "status": "stopped", "message": str(error)}
