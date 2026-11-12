"""Qualify native arithmetic bindings by changing one fixture input per subtransaction."""

from family_formula_dependencies import dependency_plan
from family_library_discovery import manifest_digest
from family_parameter_transfer_effects import set_reviewed_value
from family_parameter_transfer_preview import reviewed_value
from probe_family_parameter_transfer import run_trial
from read_family_library import WorkerUnresolved, parameter_values
from read_family_parameter_transfer import transfer_state
from read_family_placement import eid

MAX_CASES = 100


def qualification_cases(plan, state, requested):
    fields = {row["name"]: row for row in state["definitions"].values()}
    graph = dependency_plan(list(fields.values()), plan["formula_order"])["dependencies"]
    formulas = {name for name in graph if fields[name]["formula"] is not None}
    leaves = {}
    impacts = {}
    for name, dependencies in graph.items():
        if name in formulas:
            leaves[name] = set().union(*(leaves[child] for child in dependencies))
            for leaf in leaves[name]:
                impacts.setdefault(leaf, set()).add(name)
        else:
            leaves[name] = {name}

    types = state["types"]
    expected = {(type_name, name) for type_name in types for name in impacts}
    if not 2 <= len(types) or not 1 <= len(expected) <= MAX_CASES or not isinstance(requested, list):
        raise ValueError("Use a bounded qualification fixture with distinct mapped types and arithmetic inputs.")
    cases = {}
    for row in requested:
        key = (row["type"], row["parameter"])
        if key not in expected or key in cases:
            raise ValueError("Use one reviewed case for each mapped type and arithmetic input.")
        type_name, name = key
        field = fields[name]
        if (field["storage"] not in {"Double", "Integer"} or field["yes_no"] or field["instance"]
                or field["read_only"] or field["reporting"]):
            raise ValueError("Use a writable numeric type input in the disposable fixture.")
        basis = types[type_name]
        value = reviewed_value(field, row["value"])
        if value == basis[field["id"]] or row["expected"].keys() != impacts[name]:
            raise ValueError("Each input must change independently and cover every affected formula.")
        outputs = {}
        for formula in sorted(formulas):
            output = fields[formula]
            original = reviewed_value(output, basis[output["id"]])
            if formula in impacts[name]:
                changed = reviewed_value(output, row["expected"][formula])
                if original == changed:
                    raise ValueError("Each expected affected formula must change in the qualification fixture.")
                outputs[formula] = changed
            else:
                outputs[formula] = original
        cases[key] = {"type": type_name, "parameter": name, "value": value, "expected": outputs}
    if cases.keys() != expected:
        raise ValueError("The qualification cases do not cover every mapped type and arithmetic input.")
    for name in impacts:
        inputs = [types[type_name][fields[name]["id"]]["value"] for type_name in types]
        if len(inputs) != len(set(inputs)):
            raise ValueError("Use distinct baseline input values across the mapped fixture types.")
    return list(cases.values()), fields


def run_cases(document, DB, plan, provisional, requested):
    cases, fields = qualification_cases(plan, provisional, requested)
    manager = document.FamilyManager
    observed = []
    for case in cases:
        family_types = {str(item.Name): item for item in manager.Types}
        parameters = {eid(item): item for item in manager.Parameters}
        family_type = family_types[case["type"]]
        field = fields[case["parameter"]]
        parameter = parameters[field["id"]]
        scope = DB.SubTransaction(document)
        status = DB.TransactionStatus
        try:
            if scope.Start() != status.Started:
                raise ValueError("Revit did not start the formula qualification subtransaction.")
            manager.CurrentType = family_type
            if str(manager.CurrentType.Name) != case["type"]:
                raise ValueError("Revit selected a different fixture type.")
            set_reviewed_value(manager, parameter, field, case["value"])
            document.Regenerate()
            values = {name: parameter_values(family_type, parameters[fields[name]["id"]], fields[name]["storage"])
                      for name in case["expected"]}
            if (parameter_values(family_type, parameter, field["storage"]) != case["value"]
                    or values != case["expected"]):
                raise ValueError("Native formula responses differ from the independently reviewed qualification values.")
            observed.append({**case, "observed": values})
        finally:
            try:
                current = scope.GetStatus()
                if current == status.Started:
                    if scope.RollBack() != status.RolledBack or scope.GetStatus() != status.RolledBack:
                        raise WorkerUnresolved("Revit did not roll back the formula qualification subtransaction.")
                elif current != status.Uninitialized:
                    raise WorkerUnresolved("The formula qualification subtransaction ended unexpectedly.")
            finally:
                scope.Dispose()
        if transfer_state(document, DB, inspect_trial=True) != provisional:
            raise WorkerUnresolved("The exact family baseline was not restored after an input perturbation.")
    return {"case_count": len(observed), "observations_sha256": manifest_digest(observed)}


def qualify(ui, document, DB, args):
    def qualification(plan, provisional):
        return run_cases(document, DB, plan, provisional, args["cases"])

    return run_trial(ui, document, DB, args["transfer"], qualification=qualification)


try:
    active_family, query_inputs, current_ui = doc, inputs, uiapp
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = qualify(current_ui, active_family, DB, query_inputs)
    except WorkerUnresolved as error:
        result = {"status": "transaction_unresolved", "reason": str(error)}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "stopped", "reason": str(error)}
