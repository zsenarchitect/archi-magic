"""Replay the verified project reload under the native Keep or Discard policy."""

from apply_family_placement import require_ask
from probe_family_parameter_reload import run_trial
from verify_family_parameter_reload import verify
from verify_family_placement_probe import unique_ids


def apply(ui, DB, document, args, context):
    require_ask(context)
    reload = args["reload"]
    _, _, trial, expected, _ = verify(reload, args["trial"], args["after"])

    def handoff(families, effects):
        if families != trial["families"] or {key: unique_ids(values) for key, values in effects.items()} != expected:
            raise ValueError("The project replay differs from the independently verified rollback trial.")
        require_ask(context)

    return run_trial(ui, DB, document, reload, handoff=handoff)


try:
    active_project, modify_inputs, current_ui = doc, inputs, uiapp
except NameError:
    pass
else:
    from pyrevit.api import DB
    result = apply(current_ui, DB, active_project, modify_inputs, __agent__)
