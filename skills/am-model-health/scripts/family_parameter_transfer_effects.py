"""Apply and verify the reviewed additions inside a guarded private family transaction."""

from family_parameter_transfer_preview import DEFINITION_FIELDS, reviewed_value
from read_family_placement import eid


def add_definitions(document, DB, plan, baseline):
    """Apply additions only. The caller owns the transaction and its rollback."""
    if not document.IsFamilyDocument or not document.IsModifiable:
        raise ValueError("Start the guarded transaction in the private family first.")

    manager = document.FamilyManager
    types = {str(family_type.Name): family_type for family_type in manager.Types}
    if types.keys() != baseline["types"].keys() or str(manager.CurrentType.Name) != baseline["current_type"]:
        raise ValueError("The native family types differ from the reviewed baseline.")

    import System

    additions = {}
    for name, row in plan["operations"].items():
        if row["action"] != "add":
            continue
        field = row["definition"]
        group = DB.ForgeTypeId(field["group"])
        spec = DB.ForgeTypeId(field["data_type"])
        parameter = manager.AddParameter(name, group, spec, False)
        if parameter is None or str(parameter.Definition.Name) != name:
            raise ValueError("Revit did not create the exact reviewed parameter.")
        additions[name] = parameter

    for type_name, family_type in types.items():
        manager.CurrentType = family_type
        if str(manager.CurrentType.Name) != type_name:
            raise ValueError("Revit did not select the exact reviewed family type.")
        for name, parameter in additions.items():
            row = plan["operations"][name]
            field = row["definition"]
            if field["formula"] is not None:
                continue
            value = reviewed_value(field, row["expected_values"][type_name])["value"]
            storage = field["storage"]
            if str(parameter.StorageType) != storage:
                raise ValueError(f"Revit assigned unexpected storage to {name!r}.")
            if storage == "Double":
                value = System.Double(value)
            elif storage == "Integer":
                value = System.Int32(value)
            manager.Set(parameter, value)
        document.Regenerate()

    for name in plan["formula_order"]:
        parameter = additions[name]
        if not parameter.CanAssignFormula:
            raise ValueError(f"Revit cannot assign the reviewed formula for {name!r}.")
        manager.SetFormula(parameter, plan["operations"][name]["definition"]["formula"])
        document.Regenerate()

    manager.CurrentType = types[baseline["current_type"]]
    document.Regenerate()
    return {name: eid(parameter) for name, parameter in additions.items()}


def verify_family_effects(plan, baseline, changed, added_ids):
    if (changed["coverage"] != {"complete": True, "issues": []}
            or changed["reference_scope"] != baseline["reference_scope"]
            or changed["dimensions"] != baseline["dimensions"]
            or changed["current_type"] != baseline["current_type"]
            or changed["types"].keys() != baseline["types"].keys()):
        raise ValueError("The trial changed family types or protected references, or the read is incomplete.")

    before = baseline["definitions"]
    after = changed["definitions"]
    additions = {name: row for name, row in plan["operations"].items() if row["action"] == "add"}
    if (additions.keys() != added_ids.keys()
            or len(set(added_ids.values())) != len(added_ids)
            or set(after) - set(before) != set(added_ids.values())
            or any(after.get(key) != field for key, field in before.items())):
        raise ValueError("The trial changed an existing definition or created an unexpected definition.")

    for name, key in added_ids.items():
        expected = additions[name]["definition"]
        actual = after[key]
        if ({field: actual[field] for field in DEFINITION_FIELDS} != expected
                or actual["id"] != key or actual["associations"] or actual["read_only"]
                or actual["formula_driven"] != (expected["formula"] is not None)):
            raise ValueError(f"The new definition for {name!r} differs from the reviewed addition.")

    observed = {}
    for type_name, before_values in baseline["types"].items():
        values = changed["types"][type_name]
        if values.keys() != after.keys() or any(values.get(key) != value for key, value in before_values.items()):
            raise ValueError(f"The trial changed an existing value in family type {type_name!r}.")
        observed[type_name] = {}
        for name, key in added_ids.items():
            row = additions[name]
            value = reviewed_value(after[key], values[key])
            if value != row["expected_values"][type_name]:
                raise ValueError(f"The trial produced an unexpected value for {name!r} in {type_name!r}.")
            observed[type_name][name] = value
    return observed


def verify_element_effects(before, after, events, owner, added_ids):
    """The only allowed changes are the family owner and the approved definition IDs."""
    allowed = set(added_ids.values())
    added = set(after) - set(before)
    deleted = set(before) - set(after)
    modified = {key for key in before.keys() & after.keys() if before[key] != after[key]}
    if len(events) != 1:
        raise ValueError("Revit did not report exactly one provisional family transaction.")
    event = events[0]
    if (added - allowed or deleted or modified - {owner}
            or set(event["added"]) - allowed or event["deleted"] or set(event["modified"]) - {owner}
            or added - set(event["added"]) or modified - set(event["modified"])
            or before[owner][0] != after[owner][0]):
        raise ValueError("The trial changed elements outside the reviewed family additions.")
