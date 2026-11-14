"""Replay a verified opening trial only under the qualified native ask policy."""

import hashlib

from issue_board import saved_document_identity
from probe_mep_penetration_placement import trial
from structural_compare_cli import checked_id, digest, identity
from verify_mep_penetration_trial import bridge_effects, require_run, verify
from verify_mep_penetration_decision import match_replay


def runtime_identity(context):
    from System.Reflection import BindingFlags

    if context is None or bool(context.Reckless):
        raise ValueError("Use the guarded pyRevit runtime.")
    assembly = context.GetType().Assembly
    service = assembly.GetType("PyRevitLabs.PyRevit.Runtime.Agent.AgentRunService", False)
    method = service.GetMethod("PolicyNameInForce", BindingFlags.Static | BindingFlags.NonPublic) if service else None
    if method is None or str(method.Invoke(None, None)) != "ask":
        raise ValueError("The installed pyRevit ask policy could not be established.")
    sha256 = hashlib.sha256()
    with open(str(assembly.Location), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


CONTROL_BEFORE = "Archi Magic approval fixture"
CONTROL_AFTER = "Archi Magic approval control"


def run_control(document, DB, context, args):
    if (str(context.Mode) != "modify" or args["disposable_fixture"] is not True or document.IsModified
            or document.IsWorkshared or document.IsFamilyDocument
            or saved_document_identity(document) != args["project"]):
        raise ValueError("Use the exact saved standalone runtime-control fixture.")
    info = document.ProjectInformation
    if info.Name != CONTROL_BEFORE:
        raise ValueError("The reviewed runtime-control fixture marker is missing.")
    runtime = runtime_identity(context)
    transaction = DB.Transaction(document, "Test native ask approval")
    status = DB.TransactionStatus
    try:
        if transaction.Start() != status.Started:
            raise ValueError("Revit did not open the runtime-control transaction.")
        info.Name = CONTROL_AFTER
        if info.Name != CONTROL_AFTER:
            raise ValueError("Revit did not retain the control value.")
        require_ask(context, runtime)
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("Revit did not hand the runtime control to the bridge.")
    finally:
        try:
            if transaction.GetStatus() == status.Started:
                transaction.RollBack()
        finally:
            transaction.Dispose()
    return {"runtime_control": "disposable_ask", "runtime_sha256": runtime, "project": args["project"],
            "control_id": str(info.Id.Value), "before": CONTROL_BEFORE, "after": CONTROL_AFTER}


def qualified_runtime(records):
    keep = require_run(records["keep"], "modify", ("committed",), approval="user")
    discard = require_run(records["discard"], "modify", ("discarded",), approval="user", status="rejected")
    actual = digest(keep["result"]["runtime_sha256"])
    if keep["run_id"] == discard["run_id"] or keep["result"] != discard["result"]:
        raise ValueError("Use separate saved native Keep and Discard controls for this exact runtime and fixture.")
    for run in (keep, discard):
        record = run["result"]
        identity(record["project"])
        expected = {"added": [], "deleted": [], "modified": [checked_id(record["control_id"])]}
        if (record.get("runtime_control") != "disposable_ask" or record.get("before") != CONTROL_BEFORE
                or record.get("after") != CONTROL_AFTER or bridge_effects(run) != expected):
            raise ValueError("The native approval control did not measure exactly one fixture element.")
    return actual


def require_ask(context, expected):
    if context is None or str(context.Mode) != "modify" or runtime_identity(context) != expected:
        raise ValueError("The current guarded ask runtime differs from the qualified controls.")


def apply(document, DB, args, context):
    qualified = qualified_runtime(args["runtime_controls"])
    require_ask(context, qualified)
    replay = args["placement"]
    proof = verify(replay["choices"], args["before"], args["trial"], args["after"])
    if (replay["preview_sha256"] != proof["preview_sha256"]
            or replay["baseline_inventory_sha256"] != proof["inventory_sha256"]):
        raise ValueError("The replay arguments differ from the verified placement trial.")
    before = require_run(args["before"], "query", ("no_changes", "rolled_back"))
    if replay["baseline_audit_token"] != before["result"]["audit_token"]:
        raise ValueError("The replay refers to a different baseline audit.")

    def handoff(rows, effects):
        match_replay(proof, rows, effects)
        require_ask(context, qualified)

    result = trial(document, DB, replay, handoff=handoff)
    return dict(result, runtime_sha256=qualified)


try:
    active_document, modify_inputs, context = doc, inputs, __agent__
except NameError:
    pass
else:
    from pyrevit.api import DB
    if str(context.Mode) == "query":
        result = {"status": "runtime_audited", "runtime_sha256": runtime_identity(context)}
    elif "runtime_control" in modify_inputs:
        result = run_control(active_document, DB, context, modify_inputs["runtime_control"])
    else:
        result = apply(active_document, DB, modify_inputs, context)
