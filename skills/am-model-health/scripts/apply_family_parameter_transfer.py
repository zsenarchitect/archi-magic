"""Replay verified private-family edits under the Revit Keep or Discard policy."""

from apply_family_placement import require_ask
from family_library_discovery import manifest_digest
from probe_family_parameter_transfer import run_trial
from verify_family_parameter_transfer import effect_roles, verify


def apply(ui, document, DB, args, context):
    require_ask(context)
    transfer = args["transfer"]
    _, baseline, trial, _ = verify(transfer, args["before"], args["trial"], args["after"])

    baseline_sha256 = manifest_digest(baseline)

    def handoff(values, events, added_ids, audit_sha256):
        current = {"effects": events, "added_ids": added_ids}
        if (audit_sha256 != baseline_sha256 or values != trial["values"]
                or effect_roles(current) != effect_roles(trial)):
            raise ValueError("The private replay differs from the independently verified trial.")
        require_ask(context)

    return run_trial(ui, document, DB, transfer, handoff=handoff, expected_audit_sha256=baseline_sha256)


try:
    active_family, modify_inputs, current_ui = doc, inputs, uiapp
except NameError:
    pass
else:
    from pyrevit.api import DB
    result = apply(current_ui, active_family, DB, modify_inputs, __agent__)
