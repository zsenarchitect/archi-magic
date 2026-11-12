# -*- coding: utf-8 -*-
"""Read and apply a reviewed batch view duplication through Revit."""
from collections import Counter
import hashlib
import json

from System import Int64
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

import view_duplicate_preview as preview

MAX_VIEWPORTS = 20000
MAX_DETAILS = 2000


def key(identifier):
    return str(identifier.Value)


def identifier(value):
    return DB.ElementId(Int64(int(preview.element_id(value))))


def modes():
    return {"plain": DB.ViewDuplicateOption.Duplicate,
            "detailing": DB.ViewDuplicateOption.WithDetailing,
            "dependent": DB.ViewDuplicateOption.AsDependent}


def document_identity(document):
    if document is None or document.IsFamilyDocument or document.IsReadOnly or not document.PathName:
        raise ValueError("Open a saved, writable project first.")
    return {"project_uid": str(document.ProjectInformation.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}


def bounded(items, limit, label, clock):
    for index, item in enumerate(items):
        if index >= limit:
            raise ValueError("The %s inventory exceeds %d elements." % (label, limit))
        if clock.ElapsedMilliseconds > 10000:
            raise ValueError("The view snapshot exceeded ten seconds. Narrow the batch.")
        yield item


def read_view(document, view_id, template=False):
    view = document.GetElement(identifier(view_id))
    if not isinstance(view, DB.View) or view.IsTemplate != template:
        raise ValueError("Revit element %s is not the expected view or template." % view_id)
    return view


def parameter_value(parameter):
    storage = str(parameter.StorageType)
    if not parameter.HasValue:
        value = None
    elif storage == "Integer":
        value = parameter.AsInteger()
    elif storage == "Double":
        value = parameter.AsDouble()
    elif storage == "String":
        value = parameter.AsString()
    elif storage == "ElementId":
        value = key(parameter.AsElementId())
    else:
        raise ValueError("A controlled setting has an unsupported storage type.")
    return [storage, value]


def parameter_map(element):
    return {key(parameter.Id): parameter for parameter in element.Parameters}


def template_state(template):
    properties = {
        "VIEW_SCALE": ("Scale", "Scale"),
        "VIEW_SCALE_PULLDOWN_IMPERIAL": ("Scale (imperial)", "Scale"),
        "VIEW_SCALE_PULLDOWN_METRIC": ("Scale (metric)", "Scale"),
        "VIEW_DETAIL_LEVEL": ("Detail level", "DetailLevel"),
        "VIEW_PARTS_VISIBILITY": ("Parts visibility", "PartsVisibility"),
        "VIEW_DISCIPLINE": ("Discipline", "Discipline"),
        "MODEL_GRAPHICS_STYLE_ANON_DRAFT": ("Visual style", "DisplayStyle"),
    }
    scalar = ("VIEW_MODEL_DISPLAY_MODE", "VIEWER_BOUND_FAR_CLIPPING", "VIEW_SHOW_HIDDEN_LINES",
              "COLOR_SCHEME_LOCATION", "PLAN_VIEW_NORTH", "VIEW_BACK_CLIPPING")
    property_ids = {str(int(getattr(DB.BuiltInParameter, name))): fields for name, fields in properties.items()}
    scalar_ids = {str(int(getattr(DB.BuiltInParameter, name))) for name in scalar}
    excluded = {key(item) for item in template.GetNonControlledTemplateParameterIds()}
    controlled = sorted({key(item) for item in template.GetTemplateParameterIds()} - excluded, key=int)
    parameters = parameter_map(template)
    settings = []
    for parameter_id in controlled:
        if parameter_id in property_ids:
            name, attribute = property_ids[parameter_id]
            value = getattr(template, attribute)
            settings.append([parameter_id, name, ["Property", str(value)]])
        elif parameter_id in scalar_ids and parameter_id in parameters:
            parameter = parameters[parameter_id]
            settings.append([parameter_id, parameter.Definition.Name, parameter_value(parameter)])
        else:
            raise ValueError("Template %s controls a setting whose live value cannot be verified (%s)."
                             % (key(template.Id), parameter_id))
    return {"id": key(template.Id), "uid": str(template.UniqueId), "name": template.Name,
            "version": str(template.VersionGuid), "settings": settings,
            "controlled_settings": [row[1] for row in settings]}


def view_identity(view):
    return {"uid": str(view.UniqueId), "name": view.Name, "view_type": str(view.ViewType),
            "version": str(view.VersionGuid), "primary_id": key(view.GetPrimaryViewId()),
            "template_id": key(view.ViewTemplateId)}


def xyz(point):
    return [float(point.X), float(point.Y), float(point.Z)]


def detail_line(document, element):
    if not isinstance(element, DB.DetailLine) or element.GroupId.Value != -1:
        return None
    curve = element.GeometryCurve
    if not isinstance(curve, DB.Line) or not curve.IsBound:
        return None
    style = element.LineStyle
    if style is None or style.GraphicsStyleCategory is None:
        return None
    if (element.GetDependentElements(None).Count or element.GetEntitySchemaGuids().Count
            or any(element.GetAdjoinedCurveElements(end).Count for end in (0, 1))):
        return None
    category = style.GraphicsStyleCategory
    style_color = category.LineColor
    pattern_id = category.GetLinePatternId(DB.GraphicsStyleType.Projection)
    segments = []
    if pattern_id.Value > 0:
        pattern = document.GetElement(pattern_id)
        if not isinstance(pattern, DB.LinePatternElement):
            return None
        segments = [[str(segment.Type), float(segment.Length)]
                    for segment in pattern.GetLinePattern().GetSegments()]
    parameters = parameter_map(element)
    return {"id": key(element.Id), "uid": str(element.UniqueId), "version": str(element.VersionGuid),
            "type": key(element.GetTypeId()),
            "style": [key(style.Id), str(style.UniqueId), category.Name,
                      [style_color.Red, style_color.Green, style_color.Blue],
                      category.GetLineWeight(DB.GraphicsStyleType.Projection),
                      key(pattern_id), segments],
            "pinned": bool(element.Pinned),
            "points": [xyz(curve.GetEndPoint(index)) for index in (0, 1)],
            "parameters": [[item, parameter_value(parameters[item])] for item in sorted(parameters, key=int)]}


def view_details(document, view, budget, clock):
    owned = DB.FilteredElementCollector(document).OwnedByView(view.Id).WhereElementIsNotElementType()
    rows = []
    for element in bounded(owned, budget, "source annotation", clock):
        row = detail_line(document, element)
        if row is None:
            return None, len(rows) + 1
        rows.append(row)
    return sorted(rows, key=lambda row: int(row["id"])), len(rows)


def owner_state(document, view):
    if not document.IsWorkshared:
        return None
    return [str(DB.WorksharingUtils.GetCheckoutStatus(document, view.Id)),
            str(DB.WorksharingUtils.GetModelUpdatesStatus(document, view.Id))]


def can_edit(state):
    return state is None or (state[0] in ("OwnedByCurrentUser", "NotOwned")
                             and state[1] in ("CurrentWithCentral", "NotYetInCentral"))


def viewports(document, family_ids, clock):
    placements = {item: [] for item in family_ids}
    if not placements:
        return placements
    collector = DB.FilteredElementCollector(document).OfClass(DB.Viewport)
    for viewport in bounded(collector, MAX_VIEWPORTS, "viewport", clock):
        view_id = key(viewport.ViewId)
        if view_id in placements:
            sheet = document.GetElement(viewport.SheetId)
            if not isinstance(sheet, DB.ViewSheet):
                raise ValueError("An affected viewport has no verifiable sheet.")
            placements[view_id].append([key(sheet.Id), str(sheet.UniqueId), sheet.SheetNumber,
                                        key(viewport.Id), str(viewport.UniqueId)])
    return {view_id: sorted(rows) for view_id, rows in placements.items()}


def read_snapshot(document, request):
    clock = Stopwatch.StartNew()
    identity = document_identity(document)
    requested = request["sources"]
    if not isinstance(requested, list) or not 1 <= len(requested) <= preview.MAX_SOURCES:
        raise ValueError("Choose one to 50 views to duplicate.")
    native_modes = modes()
    template = read_view(document, request["template_id"], template=True)
    states = {key(template.Id): template_state(template)}
    names = sorted(view.Name for view in bounded(DB.FilteredElementCollector(document).OfClass(DB.View),
                                                 preview.MAX_VIEWS, "view", clock))
    requested_modes = {}
    for row in requested:
        source_id = preview.element_id(row["id"])
        mode = row["mode"]
        if mode not in native_modes:
            raise ValueError("Choose plain, detailing, or dependent duplication.")
        requested_modes.setdefault(source_id, set()).add(mode)

    marker_hosts = {}
    for index, row in enumerate(requested, 1):
        if "marker_host_id" in row:
            if row["mode"] != "dependent":
                raise ValueError("A marker host is only valid for a dependent duplicate.")
            host_id = preview.element_id(row["marker_host_id"])
            if host_id not in marker_hosts:
                host = read_view(document, host_id)
                state = view_identity(host)
                ownership = owner_state(document, host)
                state.update(ownership=ownership, writable=can_edit(ownership))
                marker_hosts[host_id] = state
        view = read_view(document, row["id"])
        name = preview.duplicate_name(request["pattern"], view.Name, index, row["id"])
        if not DB.NamingUtils.IsValidName(name):
            raise ValueError("Generated name %d contains prohibited Revit characters." % index)

    sources = {}
    family_views = {}
    total_members = 0
    detail_budget = MAX_DETAILS
    for source_id, requested_options in requested_modes.items():
        view = read_view(document, source_id)
        row = view_identity(view)
        row["allowed_modes"] = sorted(option for option in requested_options
                                      if view.CanViewBeDuplicated(native_modes[option]))
        row["template_allowed"] = bool(view.IsValidViewTemplate(template.Id))
        if "detailing" in requested_options:
            details, count = view_details(document, view, detail_budget, clock)
            detail_budget -= count
            row.update(details=details, details_complete=details is not None)
        sources[source_id] = row
        if "dependent" not in requested_options:
            continue
        primary_id = row["primary_id"] if row["primary_id"] != "-1" else source_id
        if primary_id not in family_views:
            primary = read_view(document, primary_id)
            remaining = preview.MAX_FAMILY - total_members - 1
            if remaining < 0:
                raise ValueError("The batch exceeds 100 existing dependent family views.")
            member_ids = [key(item) for item in bounded(primary.GetDependentViewIds(), remaining,
                                                       "dependent family", clock)]
            family_views[primary_id] = [primary] + [read_view(document, item) for item in member_ids]
            total_members += len(member_ids) + 1

    placements = viewports(document, [key(view.Id) for views in family_views.values() for view in views], clock)
    families = {}
    for primary_id, views in family_views.items():
        members = {}
        for view in views:
            row = view_identity(view)
            old_id = row["template_id"]
            if old_id != "-1" and old_id not in states:
                states[old_id] = template_state(read_view(document, old_id, template=True))
            old = states.get(old_id, {})
            located = placements[key(view.Id)]
            if bool(located) != (str(view.GetPlacementOnSheetStatus()) != "NotPlaced"):
                raise ValueError("An affected view's sheet placement could not be verified.")
            ownership = owner_state(document, view)
            row.update(template_name=old.get("name"), controlled_settings=old.get("controlled_settings", []),
                       placement_ids=located, sheets=[item[2] for item in located],
                       ownership=ownership, writable=can_edit(ownership))
            members[key(view.Id)] = row
        families[primary_id] = {"members": members,
                                "template_allowed": bool(views[0].IsValidViewTemplate(template.Id))}
    if clock.ElapsedMilliseconds > 10000:
        raise ValueError("The view snapshot exceeded ten seconds. Narrow the batch.")
    snapshot = {"kind": "view-duplicate-snapshot", "complete": True, "document": identity,
                "request": request, "names": names, "template": states[key(template.Id)], "sources": sources,
                "families": families, "template_states": states, "marker_hosts": marker_hosts}
    preview.snapshot_digest(snapshot)
    return snapshot


TRANSACTION_NAME = "Duplicate Revit views"


def approved_snapshot(document, plan, display):
    snapshot = read_snapshot(document, plan["request"])
    actual_plan, actual_display = preview.compute(snapshot)
    if actual_plan["blocked"] or plan != actual_plan or display.get("blocked") is not False:
        raise ValueError("The approved plan is blocked or its model inputs changed.")
    if any(warning.get("blocking") for warning in display["warnings"]):
        raise ValueError("The displayed preview has a blocking warning.")
    for field in actual_display:
        if field not in ("warnings", "notes") and display.get(field) != actual_display[field]:
            raise ValueError("The displayed preview differs from the approved plan.")
    notes = [note for note in display.get("notes", []) if not note.startswith("Ownership: ")]
    if notes != actual_display.get("notes", []):
        raise ValueError("The displayed marker scope differs from the approved plan.")
    ownership = display.get("ownership")
    if ownership and (ownership["owned_by_others"] or ownership["updated_in_central"]):
        raise ValueError("The displayed preview has a worksharing conflict.")
    return snapshot


def verify_view(view, step, template_id, view_type):
    row = view_identity(view)
    if (row["name"] != step["name"] or row["view_type"] != view_type
            or row["primary_id"] != step["primary_id"] or row["template_id"] != template_id):
        raise ValueError("A duplicated view differs from its approved name, type, template or primary.")
    return {"id": key(view.Id), "uid": row["uid"], "name": row["name"],
            "view_type": row["view_type"], "primary_id": row["primary_id"],
            "template_id": row["template_id"], "mode": step["mode"]}


def create_duplicates(document, plan, snapshot):
    template = identifier(plan["request"]["template_id"])
    options = modes()
    created = []
    for step in plan["steps"]:
        source = read_view(document, step["source_id"])
        if str(source.UniqueId) != step["source_uid"] or not source.CanViewBeDuplicated(options[step["mode"]]):
            raise ValueError("A source view changed or can no longer be duplicated.")
        primary_id = step["primary_id"]
        if primary_id != "-1" and not can_edit(owner_state(document, read_view(document, primary_id))):
            raise ValueError("Worksharing status blocks the primary view.")
        if "marker_host_id" in step:
            host = read_view(document, step["marker_host_id"])
            if not can_edit(owner_state(document, host)):
                raise ValueError("Worksharing status blocks the dependent-view marker host.")
        new_view = document.GetElement(source.Duplicate(options[step["mode"]]))
        if not isinstance(new_view, DB.View) or not new_view.IsValidViewTemplate(template):
            raise ValueError("A duplicated view is incompatible with the template.")
        new_view.Name = step["name"]
        if primary_id == "-1":
            new_view.ViewTemplateId = template
        created.append((new_view, step))
    for primary_id, members in plan["family_changes"].items():
        if any(not can_edit(owner_state(document, read_view(document, item))) for item in members):
            raise ValueError("Worksharing status blocks an affected dependent family member.")
        primary = read_view(document, primary_id)
        if not primary.IsValidViewTemplate(template):
            raise ValueError("The template is incompatible with the primary view.")
        primary.ViewTemplateId = template
    document.Regenerate()
    return [verify_view(view, step, key(template), snapshot["sources"][step["source_id"]]["view_type"])
            for view, step in created]


def verify_families(document, snapshot, new_views):
    states = {}
    for primary_id, family in snapshot["families"].items():
        old = family["members"]
        primary = read_view(document, primary_id)
        expected = set(old) - {primary_id}
        expected.update(row["id"] for row in new_views if row["primary_id"] == primary_id)
        current = primary.GetDependentViewIds()
        if current.Count != len(expected) or {key(item) for item in current} != expected:
            raise ValueError("The primary view's dependent membership differs from the approved batch.")
        for view_id, prior in old.items():
            row = view_identity(read_view(document, view_id))
            if (row["template_id"] != snapshot["template"]["id"]
                    or any(row[field] != prior[field] for field in ("name", "uid", "primary_id", "view_type"))):
                raise ValueError("An existing dependent family view differs from its approved state.")
            states[view_id] = {field: row[field] for field in ("uid", "name", "template_id", "primary_id", "view_type")}
    placements = viewports(document, states, Stopwatch.StartNew())
    for family in snapshot["families"].values():
        for view_id, prior in family["members"].items():
            if placements[view_id] != prior["placement_ids"]:
                raise ValueError("A reviewed view changed its sheet placement.")
            states[view_id]["placement_ids"] = placements[view_id]
    return states


def capture_changes(events):
    if len(events) != 1 or events[0] is None:
        raise ValueError("Revit did not provide one complete transaction change record.")
    added, modified, deleted = events[0]
    return {"added": sorted(added, key=int), "modified": sorted(modified, key=int),
            "deleted": sorted(deleted, key=int)}


def identify_marker(document, element, expected_host, expected_views):
    if (element is None or not element.ViewSpecific or key(element.OwnerViewId) != expected_host
            or element.Category is None):
        return None
    if isinstance(element, DB.ElevationMarker):
        targets = {key(element.GetViewId(index)) for index in range(element.MaximumViewCount)} - {"-1"}
    else:
        try:
            target = DB.ReferenceableViewUtils.GetReferencedViewId(document, element.Id)
        except Exception:
            return None
        targets = {key(target)}
    if len(targets) != 1 or not targets.issubset(expected_views):
        return None
    return {"id": key(element.Id), "uid": str(element.UniqueId), "host_id": expected_host,
            "view_id": next(iter(targets)), "category_id": key(element.Category.Id)}


def classify_changes(document, plan, snapshot, new_views, changes):
    if changes["deleted"] or not set(changes["modified"]).issubset(plan["ownership_ids"]):
        raise ValueError("Revit deleted or modified an element outside the approved scope.")
    new_by_id = {row["id"]: row for row in new_views}
    if not set(new_by_id).issubset(changes["added"]):
        raise ValueError("Revit change tracking did not identify every new view.")
    details, markers = [], []
    marker_targets = {}
    for row, step in zip(new_views, plan["steps"]):
        if "marker_host_id" in step:
            marker_targets.setdefault(step["marker_host_id"], set()).add(row["id"])
    for item in set(changes["added"]) - set(new_by_id):
        element = document.GetElement(identifier(item))
        owner = new_by_id.get(key(element.OwnerViewId)) if element is not None else None
        if owner and owner["mode"] == "detailing":
            detail = detail_line(document, element)
            if detail is not None:
                details.append({"id": item, "uid": detail["uid"], "view_id": owner["id"],
                                "points": detail["points"], "style": detail["style"]})
                continue
        if element is not None:
            host_id = key(element.OwnerViewId)
            if host_id in marker_targets:
                marker = identify_marker(document, element, host_id, marker_targets[host_id])
                if marker is not None:
                    markers.append(marker)
                    continue
            raise ValueError("Unverified addition %s has owner view %s. Review its scope and repeat the dry run."
                             % (item, host_id))
        raise ValueError("Revit added an unavailable element (%s)." % item)
    for view, step in zip(new_views, plan["steps"]):
        if step["mode"] != "detailing":
            continue
        original = snapshot["sources"][step["source_id"]]["details"]
        copied = [row for row in details if row["view_id"] == view["id"]]
        signature = lambda row: json.dumps([row["points"], row["style"]], ensure_ascii=True)
        if Counter(map(signature, original)) != Counter(map(signature, copied)):
            raise ValueError("The copied detail geometry or style differs from the source.")
    return (sorted(details, key=lambda row: int(row["id"])),
            sorted(markers, key=lambda row: int(row["id"])))


def rollback(transaction, group):
    try:
        state = transaction.GetStatus()
        if state == DB.TransactionStatus.Started:
            state = transaction.RollBack()
        if state == DB.TransactionStatus.Pending:
            return False
        return (group.GetStatus() == DB.TransactionStatus.Started
                and group.RollBack() == DB.TransactionStatus.RolledBack)
    except Exception:
        return False


def apply(document, plan, display):
    snapshot = approved_snapshot(document, plan, display)
    application = document.Application
    events = []

    def record_change(_sender, event):
        try:
            if (len(events) or not event.GetDocument().Equals(document)
                    or list(event.GetTransactionNames()) != [TRANSACTION_NAME]):
                events.append(None)
                return
            added = event.GetAddedElementIds()
            modified = event.GetModifiedElementIds()
            deleted = event.GetDeletedElementIds()
            if added.Count > MAX_DETAILS or modified.Count > len(plan["ownership_ids"]) or deleted.Count:
                events.append(None)
                return
            events.append(tuple({key(item) for item in items} for items in (added, modified, deleted)))
        except Exception:
            events.append(None)

    group = DB.TransactionGroup(document, TRANSACTION_NAME)
    transaction = DB.Transaction(document, TRANSACTION_NAME)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the change group."}
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the duplication transaction.")
        application.DocumentChanged += record_change
        try:
            views = create_duplicates(document, plan, snapshot)
            state = transaction.Commit()
        finally:
            application.DocumentChanged -= record_change
        if state != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not commit the duplication transaction.")
        changes = capture_changes(events)
        details, markers = classify_changes(document, plan, snapshot, views, changes)
        families = verify_families(document, snapshot, views)
        for row, step in zip(views, plan["steps"]):
            current = verify_view(read_view(document, row["id"]), step, row["template_id"], row["view_type"])
            if current != row:
                raise ValueError("Revit changed a duplicated view while committing it.")
        receipt = {"document": snapshot["document"], "basis_sha256": plan["basis_sha256"],
                   "views": views, "families": families, "details": details, "markers": markers, "changes": changes}
        payload = json.dumps(receipt, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        preview.check_receipt(plan, receipt)
        if len(payload) > preview.MAX_BYTES:
            raise ValueError("The duplication receipt exceeds 2 MiB.")
        if group.Assimilate() != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not finish the duplication group.")
        return {"status": "ok", "receipt_json": payload}
    except Exception as error:
        if rollback(transaction, group):
            return {"status": "stopped", "message": str(error)}
        return {"status": "transaction_unresolved", "message": "Check the Revit transaction before continuing."}


def verify_kept(document, plan, receipt):
    preview.check_receipt(plan, receipt)
    if document_identity(document) != plan["document"]:
        raise ValueError("Open the saved project that contains the kept batch.")
    created = []
    for step, expected in zip(plan["steps"], receipt["views"]):
        view = read_view(document, expected["id"])
        row = verify_view(view, step, plan["request"]["template_id"], expected["view_type"])
        if row != expected:
            raise ValueError("The kept view identity differs from the transaction receipt.")
        created.append(row)

    families = {}
    for view_id, expected in receipt["families"].items():
        current = view_identity(read_view(document, view_id))
        families[view_id] = {field: current[field] for field in
                             ("uid", "name", "template_id", "primary_id", "view_type")}
    placements = viewports(document, families, Stopwatch.StartNew())
    for item, row in families.items():
        row["placement_ids"] = placements[item]
    if families != receipt["families"]:
        raise ValueError("An existing family view changed after Keep.")
    for primary_id in {row["primary_id"] for row in created} - {"-1"}:
        expected = {item for item, row in families.items() if row["primary_id"] == primary_id}
        expected.update(row["id"] for row in created if row["primary_id"] == primary_id)
        primary = read_view(document, primary_id)
        actual = primary.GetDependentViewIds()
        if actual.Count != len(expected) or {key(item) for item in actual} != expected:
            raise ValueError("The kept dependent family membership differs from the receipt.")

    details = []
    for expected in receipt["details"]:
        element = document.GetElement(identifier(expected["id"]))
        detail = detail_line(document, element)
        if detail is None:
            raise ValueError("A kept detail line could not be verified.")
        row = {"id": detail["id"], "uid": detail["uid"], "points": detail["points"],
               "style": detail["style"], "view_id": key(element.OwnerViewId)}
        if row != expected:
            raise ValueError("A kept detail line differs from the receipt.")
        details.append(row)
    markers = []
    for expected in receipt["markers"]:
        element = document.GetElement(identifier(expected["id"]))
        row = identify_marker(document, element, expected["host_id"], {expected["view_id"]})
        if row != expected:
            raise ValueError("A kept dependent-view marker differs from the receipt.")
        markers.append(row)
    result = {"document": document_identity(document), "basis_sha256": plan["basis_sha256"],
              "views": created, "families": families, "details": details, "markers": markers,
              "changes": receipt["changes"]}
    payload = json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(payload) > preview.MAX_BYTES:
        raise ValueError("The independent verification exceeds 2 MiB.")
    return {"status": "ok", "verification_json": payload}


def main(document, args):
    mode = args.get("mode", "read")
    if mode == "apply":
        return apply(document, args["plan"], args["preview"])
    if mode == "verify":
        return verify_kept(document, args["plan"], args["receipt"])
    if mode != "read":
        raise ValueError("Choose read, apply, or verify.")
    snapshot = read_snapshot(document, args["request"])
    return {"status": "ok", "snapshot_json": json.dumps(snapshot, ensure_ascii=True, sort_keys=True,
                                                        separators=(",", ":"), allow_nan=False)}


if "inputs" in globals():
    try:
        result = main(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception:
        result = {"status": "stopped", "message": "Revit could not verify the view duplication inputs."}
