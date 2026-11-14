"""Independently read a private family before or after a transfer run."""

from probe_family_parameter_transfer import audit_result
from read_family_library import WorkerUnresolved


try:
    active_family, query_inputs, current_ui = doc, inputs, uiapp
except NameError:
    pass
else:
    from pyrevit.api import DB
    try:
        result = audit_result(current_ui, active_family, DB, query_inputs)
    except WorkerUnresolved as error:
        result = {"status": "worker_unresolved", "reason": str(error)}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "blocked", "reason": str(error)}
