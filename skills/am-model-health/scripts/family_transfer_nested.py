"""Read complete nested-family identities before allowing an ordinary reload callback."""

from read_family_library import WorkerUnresolved, bounded_text
from read_family_placement import eid

MAX_NESTED = 100
MAX_DEPTH = 4


def family_identity(family):
    if family.FamilyCategory is None:
        raise ValueError("A family has no readable category.")
    return {"id": eid(family), "uid": bounded_text(family.UniqueId), "name": bounded_text(family.Name),
            "category_id": eid(family.FamilyCategory)}


def nested_families(ui, DB, document):
    """Inspect every embedded family in an independent document and close it unsaved."""
    remaining = MAX_NESTED
    application = ui.Application
    active = ui.ActiveUIDocument

    def active_unchanged():
        current = ui.ActiveUIDocument
        if active is None:
            return current is None
        return current is not None and current.Document.Equals(active.Document)

    def collect(parent, ancestors):
        nonlocal remaining
        if (not parent.IsFamilyDocument or parent.IsModifiable or parent.IsModified
                or parent.IsWorkshared or parent.IsModelInCloud):
            raise ValueError("Use unmodified standalone family documents for nested inspection.")
        owner = parent.OwnerFamily
        families = {}
        for family in DB.FilteredElementCollector(parent).OfClass(DB.Family):
            if eid(family) == eid(owner):
                continue
            row = family_identity(family)
            if row["id"] in families or len(families) >= remaining:
                raise ValueError("The complete nested family inventory exceeds its limit.")
            families[row["id"]] = (family, row)
        for position, symbol in enumerate(DB.FilteredElementCollector(parent).OfClass(DB.FamilySymbol)):
            if position >= 10000 or (eid(symbol.Family) != eid(owner) and eid(symbol.Family) not in families):
                raise ValueError("A nested symbol is outside the complete family inventory.")

        rows = []
        for family, row in families.values():
            folded = row["name"].casefold()
            if (folded in ancestors or len(ancestors) > MAX_DEPTH or remaining <= 0
                    or not family.IsEditable or family.IsInPlace):
                raise ValueError("A nested family cannot be inspected independently within the supported bounds.")
            remaining -= 1
            before = list(application.Documents)
            child = None
            try:
                opened = parent.EditFamily(family)
                if opened is None or any(opened.Equals(item) for item in before):
                    raise WorkerUnresolved("Revit did not open an independent nested family document.")
                child = opened
                current = list(application.Documents)
                if (len(current) != len(before) + 1 or child not in current
                        or any(item not in current for item in before) or not active_unchanged()):
                    raise WorkerUnresolved("Revit changed the document scope during nested inspection.")
                extracted = family_identity(child.OwnerFamily)
                if any(extracted[key] != row[key] for key in ("name", "category_id")):
                    raise ValueError("A nested family extraction differs from the embedded definition.")
                children = collect(child, ancestors | {folded})
            finally:
                try:
                    closed = child is None or child.Close(False)
                    current = list(application.Documents)
                    restored = (len(current) == len(before) and all(item in current for item in before)
                                and active_unchanged())
                except Exception as error:
                    raise WorkerUnresolved("Revit could not confirm closure of the nested inspection.") from error
                if not closed or not restored:
                    raise WorkerUnresolved("Revit did not restore the original documents after nested inspection.")
            rows.append({**row, "children": children})
        if parent.IsModified or parent.IsModifiable:
            raise WorkerUnresolved("A family changed during nested inspection.")
        return sorted(rows, key=lambda row: (row["name"].casefold(), row["uid"]))

    return collect(document, {str(document.OwnerFamily.Name).casefold()})


def nested_names(rows):
    names = set()
    remaining = MAX_NESTED

    def collect(nodes, depth):
        nonlocal remaining
        for node in nodes:
            remaining -= 1
            if remaining < 0 or depth > MAX_DEPTH:
                raise ValueError("The recorded nested family inventory exceeds its limits.")
            names.add(node["name"].casefold())
            collect(node["children"], depth + 1)

    collect(rows, 1)
    return names
