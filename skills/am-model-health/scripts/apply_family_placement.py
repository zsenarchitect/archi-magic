"""Hand a rechecked family placement to pyRevit's in-Revit Keep/Discard prompt."""

import math

from probe_family_placement import POSITION_TOLERANCE_FT, ROTATION_TOLERANCE_DEG, trial
from verify_family_placement_probe import verify


def require_ask(context):
    """Read the same in-force policy used by pyRevit's get_context."""
    if context is None or str(context.Mode) != "modify" or bool(context.Reckless):
        raise ValueError("Use a guarded pyRevit modify run with ask policy.")
    from System.Reflection import BindingFlags
    assembly = context.GetType().Assembly
    service = assembly.GetType("PyRevitLabs.PyRevit.Runtime.Agent.AgentRunService", False)
    method = service.GetMethod("PolicyNameInForce", BindingFlags.Static | BindingFlags.NonPublic) if service else None
    if method is None or str(method.Invoke(None, None)) != "ask":
        raise ValueError("The live pyRevit ask policy could not be established.")


def validate_handoff(proof, placement, effects):
    old_pose = proof["placement"]
    angle = (placement["rotation_deg"] - old_pose["rotation_deg"] + 180) % 360 - 180
    if (not math.isfinite(angle) or abs(angle) > ROTATION_TOLERANCE_DEG
            or any(abs(actual - expected) > POSITION_TOLERANCE_FT
                   for actual, expected in zip(placement["xyz_ft"], old_pose["xyz_ft"]))
            or placement["level_id"] != old_pose["level_id"]
            or (proof["action"] == "reuse" and placement["symbol_id"] != old_pose["symbol_id"])
            or len(effects["added"]) != len(proof["effects"]["added"])
            or sorted(effects["modified"]) != sorted(proof["effects"]["modified"])
            or effects["deleted"]):
        raise ValueError("The provisional placement differs from the verified rollback trial.")


def apply(document, DB, args, context):
    if set(args) != {"placement", "before", "trial", "after"}:
        raise ValueError("Supply the reviewed placement and all three rollback run records.")
    require_ask(context)
    placement = args["placement"]
    proof = verify(placement["preview"], args["before"], args["trial"], args["after"])
    baseline = args["before"].get("response", args["before"])
    if placement["baseline_audit_token"] != baseline["result"]["audit_token"]:
        raise ValueError("The placement uses a different baseline audit.")

    def handoff(measured, effects):
        validate_handoff(proof, measured, effects)
        require_ask(context)

    return trial(document, DB, placement, handoff=handoff, expected_state=proof)


try:
    active_project, modify_inputs = doc, inputs
except NameError:
    pass
else:
    from pyrevit.api import DB
    # Let errors propagate so pyRevit rolls back its outer group.
    result = apply(active_project, DB, modify_inputs, __agent__)
