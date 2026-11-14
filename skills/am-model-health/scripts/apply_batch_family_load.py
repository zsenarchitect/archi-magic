"""Hand an independently verified batch family load to Revit's Keep/Discard prompt."""

from apply_family_placement import require_ask
from probe_batch_family_load import run_trial
from verify_batch_family_load import verify


def apply(document, DB, args, context):
    if set(args) != {"load_run", "before", "trial", "after"}:
        raise ValueError("Supply the reviewed load and all three saved rollback records.")
    require_ask(context)
    requested = args["load_run"]
    proof = verify(requested["lookup"], requested["staging"], requested["preview"],
                   args["before"], args["trial"], args["after"])
    if requested["baseline_audit_token"] != proof["baseline_audit_token"]:
        raise ValueError("The replay uses a different project audit.")
    return run_trial(document, DB, requested, handoff=lambda: require_ask(context), expected_state=proof)


try:
    active_project, modify_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    result = apply(active_project, DB, modify_inputs, __agent__)
