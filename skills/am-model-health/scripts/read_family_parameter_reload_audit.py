"""Independently inspect the exact project after a kept family reload."""

from uuid import uuid4

from probe_family_parameter_reload import inspect_batch, reviewed_reload
from read_family_library import WorkerUnresolved
from read_family_placement import eid, inventory_record, saved_project_identity


def read_kept(ui, DB, document, args):
    reload = args["reload"]
    baseline, _ = reviewed_reload(reload)
    project = saved_project_identity(document, require_unmodified=False)
    app = ui.Application
    active = ui.ActiveUIDocument
    if (project != baseline["project"] or document.IsModifiable or list(app.Documents) != [document]
            or active is None or not active.Document.Equals(document) or str(app.VersionNumber) != "2026"
            or str(app.VersionBuild) != baseline["revit_build"]):
        raise ValueError("Inspect the same standalone project and Revit build after Keep.")
    bound = []
    loaded = {}
    for row in reload["preview"]["rows"]:
        identifier = row["loaded_family"]["id"]
        family = document.GetElement(DB.ElementId(int(identifier)))
        if (not isinstance(family, DB.Family) or eid(family) != identifier
                or str(family.UniqueId) != row["loaded_family"]["uid"]
                or not family.IsEditable or family.IsInPlace):
            raise ValueError("A reviewed family is missing after Keep.")
        bound.append((None, row["receipt"]))
        loaded[identifier] = family
    families, current = inspect_batch(ui, DB, document, baseline, bound, loaded)
    return {"status": "ok", "audit_token": uuid4().hex, "trial_transaction": args["trial_transaction"],
            "preview_sha256": reload["preview"]["preview_sha256"], "project": project,
            "inventory": inventory_record(current), "families": families}


try:
    active_project, query_inputs, current_ui = doc, inputs, uiapp
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = read_kept(current_ui, DB, active_project, query_inputs)
    except WorkerUnresolved as error:
        result = {"status": "worker_unresolved", "reason": str(error)}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "blocked", "reason": str(error)}
