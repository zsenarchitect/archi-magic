# -*- coding: utf-8 -*-
"""Read clash bounds and create a reviewed Revit clash section view."""
import itertools
import json
import re
from uuid import UUID

from System import Int64
from System.Diagnostics import Stopwatch
from pyrevit.api import DB

import issue_board as ib
from clash_section import FRAME, section_view_preview

LIMIT = 2 * 1024 * 1024
TEXT = (str, type(u""))


def element_id(value):
    if (not isinstance(value, TEXT) or not re.match(r"^[1-9][0-9]{0,18}\Z", value)
            or int(value) > 2**63 - 1):
        raise ValueError("Supply positive decimal-string Revit element IDs.")
    return DB.ElementId(Int64(int(value)))


def budget_check(clock):
    if clock.ElapsedMilliseconds >= 10000:
        raise ValueError("Clash bounds exceeded the ten-second read budget.")


def xyz(point):
    return [float(point.X), float(point.Y), float(point.Z)]


def host_bounds(element, transform):
    box = element.get_BoundingBox(None)
    if box is None or not box.Enabled:
        return None
    minimum, maximum = xyz(box.Min), xyz(box.Max)
    if any(not -1e6 <= low <= high <= 1e6 for low, high in zip(minimum, maximum)) or minimum == maximum:
        return None
    # Transform all corners. Transforming only Min and Max loses rotated or mirrored extents.
    points = [xyz(transform.OfPoint(box.Transform.OfPoint(DB.XYZ(*corner))))
              for corner in itertools.product(*zip(minimum, maximum))]
    if any(not -1e6 <= value <= 1e6 for point in points for value in point):
        return None
    low = [min(point[axis] for point in points) for axis in range(3)]
    high = [max(point[axis] for point in points) for axis in range(3)]
    if low == high or any(not 0 <= b - a <= 1000 for a, b in zip(low, high)):
        return None
    return low, high


def read_scope(host, scope, view, clock):
    prefix = scope["path_prefix"]
    if (not isinstance(prefix, list) or not 1 <= len(prefix) <= 20
            or any(not isinstance(node, TEXT) or not node.strip() or len(node) > 500 for node in prefix)):
        raise ValueError("Supply one to 20 reviewed selection-tree nodes for each source.")
    raw_ids = scope["element_ids"]
    if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 600 or len(set(raw_ids)) != len(raw_ids):
        raise ValueError("Supply one to 600 distinct reviewed element IDs per source.")
    ids = [element_id(value) for value in raw_ids]

    document, transform, source = host, DB.Transform.Identity, "host"
    link_id = scope.get("link_instance_id")
    link_uid = scope.get("link_instance_uid")
    if link_id is not None:
        link = host.GetElement(element_id(link_id))
        if not isinstance(link, DB.RevitLinkInstance) or not link_uid or str(link.UniqueId) != link_uid:
            raise ValueError("A reviewed direct link instance is missing or changed identity.")
        document = link.GetLinkDocument()
        source = "link:" + link_uid
        if document is not None:
            transform = link.GetTotalTransform()
    elif link_uid is not None:
        raise ValueError("A link UID requires its reviewed host element ID.")

    result = {"model_id": source, "path_prefix": [node.strip() for node in prefix],
              "link_instance_id": link_id, "link_instance_uid": link_uid,
              "document_identity": ib.saved_document_identity(document) if document is not None else None,
              "elements": {}, "unresolved": {}}
    budget_check(clock)
    if document is None:
        result["unresolved"] = {value: "link_unloaded" for value in raw_ids}
        return result
    if link_id is not None and link.IsHidden(view):
        result["unresolved"] = {value: "link_hidden_in_active_view" for value in raw_ids}
        return result
    for value, native in zip(raw_ids, ids):
        budget_check(clock)
        element = document.GetElement(native)
        if element is None:
            result["unresolved"][value] = "element_missing"
            continue
        if (isinstance(element, (DB.ElementType, DB.RevitLinkInstance)) or element.ViewSpecific
                or element.Category is None or not element.UniqueId):
            result["unresolved"][value] = "unsupported_element"
            continue
        if link_id is None and element.IsHidden(view):
            result["unresolved"][value] = "element_hidden_in_active_view"
            continue
        bounds = host_bounds(element, transform)
        if bounds is None:
            result["unresolved"][value] = "bounds_unavailable"
            continue
        low, high = bounds
        result["elements"][value] = {"unique_id": str(element.UniqueId), "min": low, "max": high}
    budget_check(clock)
    return result


def read_view_target(host, request, clock):
    clash_guid = request["clash_guid"]
    if not isinstance(clash_guid, TEXT) or str(UUID(str(clash_guid))) != clash_guid:
        raise ValueError("Choose one canonical Navisworks clash GUID.")
    view_type_id = request["view_type_id"]
    view_type = host.GetElement(element_id(view_type_id))
    if (not isinstance(view_type, DB.ViewFamilyType) or view_type.ViewFamily != DB.ViewFamily.ThreeDimensional
            or not view_type.UniqueId):
        raise ValueError("Choose a reviewed orthographic 3D view type.")
    name = "Clash " + clash_guid
    if not DB.NamingUtils.IsValidName(name):
        raise ValueError("Revit rejected the proposed clash view name.")
    matches = []
    for index, view in enumerate(DB.FilteredElementCollector(host).OfClass(DB.View)):
        budget_check(clock)
        if index >= 10000:
            raise ValueError("More than 10,000 view names. The view name inventory is incomplete.")
        if view.Name.upper() == name.upper():
            matches.append({"id": str(view.Id.Value), "uid": str(view.UniqueId), "name": view.Name})
    return {"clash_guid": clash_guid, "name": name, "view_type_id": view_type_id,
            "view_type_uid": str(view_type.UniqueId), "default_template_id": str(view_type.DefaultTemplateId.Value),
            "name_conflicts": sorted(matches, key=lambda item: int(item["id"]))}


def main(host, request):
    if host is None or host.IsFamilyDocument or host.ActiveView is None:
        raise ValueError("Open a saved Revit project with a model view.")
    clock = Stopwatch.StartNew()
    identity = ib.saved_document_identity(host)
    view = host.ActiveView
    if not view.UniqueId or str(view.UniqueId) != request["active_view_uid"] or view.IsTemplate:
        raise ValueError("The current view differs from the reviewed active view.")
    sources = request["sources"]
    if (not isinstance(sources, list) or not 1 <= len(sources) <= 32
            or any(not isinstance(item["element_ids"], list) for item in sources)
            or sum(len(item["element_ids"]) for item in sources) > 600):
        raise ValueError("Supply one to 32 reviewed sources and up to 600 total element IDs.")
    results = [read_scope(host, item, view, clock) for item in sources]
    prefixes = {tuple(item["path_prefix"]) for item in results}
    if len(prefixes) != len(results) or len({item["model_id"] for item in results}) != len(results):
        raise ValueError("Use one unique tree prefix per reviewed host or link instance.")
    if any(first != second and first[:len(second)] == second for first in prefixes for second in prefixes):
        raise ValueError("Selection-tree source prefixes must not overlap.")
    payload = {"kind": "navisworks-revit-snapshot", "coordinate_frame": FRAME,
               "host_id": identity["project_uid"] + "/" + identity["path_sha256"],
               "host_identity": identity, "active_view_uid": str(view.UniqueId), "sources": results}
    if "view_request" in request:
        payload["view_target"] = read_view_target(host, request["view_request"], clock)
        payload["request"] = request
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    budget_check(clock)
    if len(encoded) > LIMIT:
        raise ValueError("Clash bounds exceeded the snapshot size limit.")
    return {"status": "ok", "snapshot_json": encoded}


TRANSACTION_NAME = "Create reviewed Navisworks clash view"


def approved_view(host, plan, display):
    fresh = json.loads(main(host, plan["request"])["snapshot_json"])
    clash = {"guid": plan["view"]["clash_guid"], "objects": plan["clash_objects"]}
    current, expected = section_view_preview(clash["guid"], [clash], fresh, plan["padding_ft"], plan["report_sha256"])
    if current["blocked"] or plan != current or display.get("blocked") is not False:
        raise ValueError("The approved view plan no longer matches Revit.")
    if any(warning.get("blocking") for warning in display["warnings"]):
        raise ValueError("The displayed clash preview is blocked.")
    for field, value in expected.items():
        if field != "warnings" and display.get(field) != value:
            raise ValueError("The displayed clash preview differs from the approved plan.")
    return current


def view_receipt(host, view, plan):
    expected = plan["view"]
    if (not isinstance(view, DB.View3D) or view.IsTemplate or view.IsPerspective
            or view.ViewTemplateId.Value != -1 or view.Name != expected["name"]
            or str(view.GetTypeId().Value) != expected["view_type_id"]
            or not view.IsSectionBoxActive or not view.UniqueId
            or ib.saved_document_identity(host) != plan["host_identity"]):
        raise ValueError("The new view differs from its approved identity or type.")
    box = view.GetSectionBox()
    low, high = xyz(box.Min), xyz(box.Max)
    if (not box.Transform.IsIdentity
            or any(not abs(actual - intended) <= 1e-7 for actual, intended in zip(low, plan["box"]["min"]))
            or any(not abs(actual - intended) <= 1e-7 for actual, intended in zip(high, plan["box"]["max"]))):
        raise ValueError("The section box differs from its approved coordinates.")
    return {"clash_guid": expected["clash_guid"], "view_id": str(view.Id.Value), "view_uid": str(view.UniqueId),
            "view_name": view.Name, "view_type_id": str(view.GetTypeId().Value),
            "box_min": low, "box_max": high, "basis_sha256": plan["basis_sha256"],
            "host_identity": plan["host_identity"]}


def rollback(transaction, group):
    try:
        state = transaction.GetStatus()
        if state == DB.TransactionStatus.Started:
            state = transaction.RollBack()
        if state == DB.TransactionStatus.Pending:
            return False
        group_state = group.GetStatus()
        if group_state == DB.TransactionStatus.Started:
            group_state = group.RollBack()
        return group_state == DB.TransactionStatus.RolledBack
    except Exception:
        return False


def apply_view(host, plan, display):
    plan = approved_view(host, plan, display)
    application = host.Application
    events = []

    def changed(_sender, event):
        try:
            if (events or not event.GetDocument().Equals(host)
                    or list(event.GetTransactionNames()) != [TRANSACTION_NAME]):
                events.append(None)
                return
            added, modified, deleted = (event.GetAddedElementIds(), event.GetModifiedElementIds(),
                                        event.GetDeletedElementIds())
            if len(added) != 1 or len(modified) or len(deleted):
                events.append(None)
                return
            events.append(str(next(iter(added)).Value))
        except Exception:
            events.append(None)

    group = DB.TransactionGroup(host, TRANSACTION_NAME)
    transaction = DB.Transaction(host, TRANSACTION_NAME)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the clash view group."}
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the clash view transaction.")
        application.DocumentChanged += changed
        try:
            view = DB.View3D.CreateIsometric(host, element_id(plan["view"]["view_type_id"]))
            view.Name = plan["view"]["name"]
            box = DB.BoundingBoxXYZ()
            box.Transform = DB.Transform.Identity
            box.Min = DB.XYZ(*plan["box"]["min"])
            box.Max = DB.XYZ(*plan["box"]["max"])
            view.SetSectionBox(box)
            view.IsSectionBoxActive = True
            host.Regenerate()
            receipt = view_receipt(host, view, plan)
            state = transaction.Commit()
        finally:
            application.DocumentChanged -= changed
        if state != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not commit the clash view transaction.")
        if events != [receipt["view_id"]]:
            raise ValueError("Revit did not report exactly one new view with no other model changes.")
        actual = host.GetElement(element_id(receipt["view_id"]))
        if view_receipt(host, actual, plan) != receipt:
            raise ValueError("Revit changed the new section view during commit.")
        payload = json.dumps(receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if group.Assimilate() != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not complete the clash view group.")
        return {"status": "ok", "receipt_json": payload}
    except Exception as error:
        if rollback(transaction, group):
            return {"status": "stopped", "message": str(error), "target_rollback_verified": True}
        return {"status": "transaction_unresolved", "message": "Check the clash view transaction before continuing."}


def run(host, args):
    mode = args.get("mode", "read")
    if mode == "apply":
        return apply_view(host, args["plan"], args["preview"])
    if mode != "read":
        raise ValueError("Choose read or apply for the clash view.")
    return main(host, args)


if "inputs" in globals():
    try:
        result = run(doc, inputs)  # noqa: F821
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit clash evidence could not be read (%s)."
                  % type(error).__name__}
