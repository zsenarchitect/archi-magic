"""Read, create, or verify sheets in the active Revit project through am-safe-change."""

import hashlib
import math

from System import Guid, Int64
from pyrevit.api import DB

import sheet_creation_preview as preview
from duplicate_views import rollback


TRANSACTION = "Create reviewed sheets"
BUILT_INS = {"SHEET_DRAWN_BY", "SHEET_CHECKED_BY", "SHEET_DESIGNED_BY", "SHEET_APPROVED_BY", "SHEET_ISSUE_DATE"}
STORAGE = {"string": "String", "integer": "Integer", "yes_no": "Integer", "double": "Double",
           "element_id": "ElementId"}


def element_key(identifier):
    return str(identifier.Value)


def element_id(value):
    return DB.ElementId(Int64(int(preview.element_id(value))))


def document_identity(document):
    if document is None or document.IsFamilyDocument or not document.PathName or document.ProjectInformation is None:
        raise ValueError("Open the saved Revit project used for the sheet list.")
    return {"project_uid": str(document.ProjectInformation.UniqueId),
            "path_sha256": hashlib.sha256(document.PathName.encode("utf-8")).hexdigest()}


def owned_title_block(document, sheet):
    collector = (DB.FilteredElementCollector(document).OwnedByView(sheet.Id)
                 .OfCategory(DB.BuiltInCategory.OST_TitleBlocks).WhereElementIsNotElementType())
    iterator = iter(collector)
    item = next(iterator, None)
    if not isinstance(item, DB.FamilyInstance) or next(iterator, None) is not None:
        raise ValueError("The sheet does not have exactly one title block instance.")
    if element_key(item.OwnerViewId) != element_key(sheet.Id):
        raise ValueError("The title block is not owned by this sheet.")
    return item


def assignment_targets(document, sheet, title_block, assignments):
    """Resolve instance destinations and convert requested values to native storage."""
    resolved = set()
    for assignment in assignments:
        owner = sheet if assignment["scope"] == "sheet" else title_block
        if owner is None:
            raise ValueError("A requested title block instance is unavailable.")
        selector = assignment["parameter"]
        key = selector["id"]
        if selector["kind"] == "guid":
            parameter = owner.get_Parameter(Guid(key))
        elif selector["kind"] == "built_in":
            if key not in BUILT_INS:
                raise ValueError("The built-in parameter is outside the sheet metadata list.")
            parameter = owner.get_Parameter(getattr(DB.BuiltInParameter, key))
        else:
            definition = document.GetElement(element_id(key))
            if (not isinstance(definition, DB.ParameterElement) or isinstance(definition, DB.GlobalParameter)
                    or str(definition.UniqueId) != selector["uid"]):
                raise ValueError("The reviewed project parameter identity is unavailable.")
            parameter = owner.get_Parameter(definition.GetDefinition())

        kind = assignment["kind"]
        if (parameter is None or parameter.IsReadOnly or parameter.StorageType.ToString() != STORAGE[kind]
                or parameter.GetAssociatedGlobalParameter().Value != -1):
            raise ValueError("An instance parameter is missing, controlled, read-only or has a different storage type.")
        target = (element_key(owner.Id), element_key(parameter.Id))
        if target in resolved:
            raise ValueError("Two reviewed parameter identifiers resolve to the same instance field.")
        resolved.add(target)

        data_type = parameter.Definition.GetDataType()
        value = assignment["value"]
        if kind == "yes_no":
            if data_type != DB.SpecTypeId.Boolean.YesNo:
                raise ValueError("The integer parameter does not have the yes/no specification.")
            value = int(value)
        elif kind == "integer" and data_type == DB.SpecTypeId.Boolean.YesNo:
            raise ValueError("Use a boolean for a yes/no parameter.")
        elif kind == "double":
            unit = DB.ForgeTypeId(assignment["unit"])
            if not DB.UnitUtils.IsMeasurableSpec(data_type) or not DB.UnitUtils.IsValidUnit(data_type, unit):
                raise ValueError("The requested unit is incompatible with the measured parameter.")
            value = DB.UnitUtils.ConvertToInternalUnits(float(value), unit)
            if not math.isfinite(value):
                raise ValueError("The converted parameter is not finite.")
        elif kind == "element_id":
            reference = document.GetElement(element_id(value["id"]))
            if (not DB.Category.IsBuiltInCategory(data_type) or not isinstance(reference, DB.FamilySymbol)
                    or str(reference.UniqueId) != value["uid"] or reference.Category is None
                    or element_key(reference.Category.Id) != value["category_id"]
                    or reference.Category.BuiltInCategory != DB.Category.GetBuiltInCategory(data_type)):
                raise ValueError("The reviewed family-type reference is unavailable or incompatible.")
            value = reference.Id
        yield parameter, kind, value


def matches_parameter(parameter, kind, value):
    if kind == "string":
        return parameter.AsString() == value
    if kind in ("integer", "yes_no"):
        return parameter.HasValue and parameter.AsInteger() == value
    if kind == "double":
        return parameter.HasValue and math.isclose(parameter.AsDouble(), value, abs_tol=1e-8, rel_tol=1e-10)
    return parameter.HasValue and element_key(parameter.AsElementId()) == element_key(value)


def read_snapshot(document, request, excluded=()):
    identity = document_identity(document)
    request = preview.normalize_request(request)
    requested = {row["number"]: row for row in request["rows"]}
    types, sheets = [], []
    collector = DB.FilteredElementCollector(document).OfClass(DB.FamilySymbol).OfCategory(DB.BuiltInCategory.OST_TitleBlocks)
    for symbol in collector:
        if len(types) >= preview.MAX_TYPES:
            raise ValueError("The complete title block type inventory exceeds its limit.")
        types.append({"id": element_key(symbol.Id), "uid": str(symbol.UniqueId), "family": symbol.FamilyName,
                      "name": DB.Element.Name.GetValue(symbol)})
    for sheet in DB.FilteredElementCollector(document).OfClass(DB.ViewSheet):
        key = element_key(sheet.Id)
        if key in excluded:
            continue
        if len(sheets) >= preview.MAX_SHEETS:
            raise ValueError("The complete sheet number inventory exceeds its limit.")
        row = {"id": key, "uid": str(sheet.UniqueId), "number": sheet.SheetNumber,
               "name": sheet.Name, "placeholder": bool(sheet.IsPlaceholder)}
        desired = requested.get(row["number"])
        if desired and not row["placeholder"]:
            try:
                title_block = owned_title_block(document, sheet)
                row["title_block_type_id"] = element_key(title_block.GetTypeId())
                assignments = desired["parameters"]
                if all(matches_parameter(*target) for target in assignment_targets(document, sheet, title_block, assignments)):
                    row["parameters"] = assignments
            except ValueError:
                # Missing or unsupported instance evidence can never mark the existing sheet as a match.
                row["parameters"] = None
        sheets.append(row)
    return {"kind": "sheet-creation-snapshot", "complete": True, "document": identity, "request": request,
            "title_blocks": sorted(types, key=lambda item: int(item["id"])),
            "sheets": sorted(sheets, key=lambda item: int(item["id"]))}


def checked_sheet(document, sheet, step):
    if (not isinstance(sheet, DB.ViewSheet) or sheet.IsPlaceholder
            or sheet.SheetNumber != step["number"] or sheet.Name != step["name"]):
        raise ValueError("Revit sheet number or name differs from the reviewed values.")
    title_block = owned_title_block(document, sheet)
    if element_key(title_block.GetTypeId()) != step["title_block_type_id"]:
        raise ValueError("The created title block type differs from the reviewed type.")
    symbol = document.GetElement(title_block.GetTypeId())
    if not isinstance(symbol, DB.FamilySymbol) or str(symbol.UniqueId) != step["title_block_type_uid"]:
        raise ValueError("The reviewed title block type identity changed.")
    targets = assignment_targets(document, sheet, title_block, step["parameters"])
    if not all(matches_parameter(*target) for target in targets):
        raise ValueError("The sheet or title block parameter differs from the reviewed value.")
    return {"sheet_id": element_key(sheet.Id), "sheet_uid": str(sheet.UniqueId),
            "title_block_id": element_key(title_block.Id), "title_block_uid": str(title_block.UniqueId)}


def apply(document, plan):
    if document is None or document.IsReadOnly:
        raise ValueError("Open the writable Revit project used for the sheet list.")
    fresh = preview.compute(read_snapshot(document, plan["request"]))
    if plan != fresh or fresh["blocked"]:
        raise ValueError("The approved sheet plan differs from the current project or request.")
    steps = fresh["steps"]
    if not steps:
        return {"status": "no_changes", "message": "There are no new sheets to create."}

    application = document.Application
    events = []

    def record_change(_sender, event):
        try:
            if (events or not event.GetDocument().Equals(document)
                    or list(event.GetTransactionNames()) != [TRANSACTION]):
                events.append(None)
                return
            added = event.GetAddedElementIds()
            if added.Count != 2 * len(steps) or event.GetModifiedElementIds().Count or event.GetDeletedElementIds().Count:
                events.append(None)
                return
            events.append({element_key(identifier) for identifier in added})
        except Exception:
            events.append(None)

    group = DB.TransactionGroup(document, TRANSACTION)
    transaction = DB.Transaction(document, TRANSACTION)
    if group.Start() != DB.TransactionStatus.Started:
        return {"status": "transaction_unresolved", "message": "Revit did not start the sheet creation group."}
    try:
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Revit did not start the sheet creation transaction.")
        application.DocumentChanged += record_change
        try:
            created = []
            for step in steps:
                sheet = DB.ViewSheet.Create(document, element_id(step["title_block_type_id"]))
                if sheet is None:
                    raise ValueError("Revit declined to create a reviewed sheet.")
                sheet.SheetNumber = step["number"]
                sheet.Name = step["name"]
                document.Regenerate()
                block = owned_title_block(document, sheet)
                for parameter, kind, value in assignment_targets(document, sheet, block, step["parameters"]):
                    if not matches_parameter(parameter, kind, value):
                        parameter.Set(value)
                        if not matches_parameter(parameter, kind, value):
                            raise ValueError("Revit declined a reviewed instance parameter value.")
                created.append((element_key(sheet.Id), element_key(block.Id)))
            state = transaction.Commit()
        finally:
            application.DocumentChanged -= record_change
        if state != DB.TransactionStatus.Committed or events != [{key for pair in created for key in pair}]:
            raise ValueError("The committed sheet changes contain unexpected or unverifiable elements.")

        records = [checked_sheet(document, document.GetElement(element_id(pair[0])), step)
                   for pair, step in zip(created, steps)]
        if [(row["sheet_id"], row["title_block_id"]) for row in records] != created:
            raise ValueError("The committed sheet or title block identity changed.")
        if preview.compute(read_snapshot(document, plan["request"], {pair[0] for pair in created})) != plan:
            raise ValueError("The original sheet or type inventory changed during creation.")
        receipt = {"document": plan["document"], "basis_sha256": plan["basis_sha256"], "created": records}
        preview.check_receipt(plan, receipt)
        payload = preview.encode(receipt)
        if group.Assimilate() != DB.TransactionStatus.Committed:
            raise ValueError("Revit did not finish the sheet creation group.")
        return {"status": "ok", "receipt_json": payload}
    except Exception as error:
        if rollback(transaction, group):
            message = str(error) if isinstance(error, ValueError) else "Revit rejected a reviewed sheet or parameter."
            return {"status": "stopped", "message": message}
        return {"status": "transaction_unresolved", "message": "Check the Revit transaction before continuing."}


def verify_kept(document, plan, receipt):
    preview.check_receipt(plan, receipt)
    if document_identity(document) != plan["document"]:
        raise ValueError("Open the saved Revit project that contains the kept sheets.")
    for expected, step in zip(receipt["created"], plan["steps"]):
        sheet = document.GetElement(element_id(expected["sheet_id"]))
        if checked_sheet(document, sheet, step) != expected:
            raise ValueError("A kept sheet or title block no longer matches its exact receipt.")
    excluded = {row["sheet_id"] for row in receipt["created"]}
    if preview.compute(read_snapshot(document, plan["request"], excluded)) != plan:
        raise ValueError("The original sheet or type inventory differs from the reviewed preview.")
    return {"status": "ok", "verification_json": preview.encode(receipt)}


def main(document, args):
    mode = args.get("mode", "read")
    if mode == "apply":
        return apply(document, args["plan"])
    if mode == "verify":
        return verify_kept(document, args["plan"], args["receipt"])
    if mode != "read":
        raise ValueError("Choose read, apply, or verify for sheet creation.")
    snapshot = read_snapshot(document, args["request"])
    return {"status": "ok", "snapshot_json": preview.encode(snapshot)}


if "doc" in globals():
    try:
        result = main(doc, inputs)
    except (KeyError, TypeError, ValueError) as error:
        result = {"status": "stopped", "message": str(error)}
    except Exception as error:
        result = {"status": "stopped", "message": "Revit could not read the sheet-creation scope.",
                  "error_type": type(error).__name__}
