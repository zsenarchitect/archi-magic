"""Transfer checked private-family edits to the guarded Revit Keep/Discard prompt."""

from apply_family_placement import require_ask
from probe_batch_family_derivation import run_trial
from verify_batch_family_derivation import verify


def apply(document, DB, args, context):
    if set(args) != {"derivation_run", "before", "trial", "after"}:
        raise ValueError("Supply the reviewed derivation and its three independent rollback records.")
    require_ask(context)
    requested = args["derivation_run"]
    proof = verify(requested["derivation"], requested["preview"], args["before"], args["trial"], args["after"])
    if not proof["effects"] or not proof["effects"][0]["modified"]:
        raise ValueError("The trial did not expose a family change for the native approval prompt.")
    if requested["working_read"] != args["before"]:
        raise ValueError("The replay uses a different working-family query.")

    def handoff(types, events):
        if types != proof["types"] or events != proof["effects"]:
            raise ValueError("The replay differs from the verified derived values or effects.")
        require_ask(context)

    return run_trial(document, DB, requested, handoff=handoff)


try:
    active_family, modify_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    result = apply(active_family, DB, modify_inputs, __agent__)
