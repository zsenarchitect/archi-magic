"""Validate the runtime envelope of a Revit probe that leaves no changes."""


def require_run(record, mode, decisions, *, approval=None, status="ok"):
    if isinstance(record, dict):
        record = record.get("response", record)
    if (not isinstance(record, dict) or record.get("mode") != mode or record.get("status") != status
            or record.get("decision") not in decisions or type(record.get("exit_code")) is not int
            or record["exit_code"] != 0 or record.get("result_truncated") is not False
            or record.get("error") is not None or record.get("approval") != approval
            or any(record.get(key) != [] for key in ("failures", "dialogs", "blocked", "warnings"))
            or record.get("unclosed_documents", [])):
        raise ValueError("Use a complete Revit record with the required status and approval, and no warnings.")
    engine = record.get("engine")
    if not isinstance(engine, dict) or engine.get("requested") != "cpython":
        raise ValueError("Use the CPython Revit run records.")
    run_id = record.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 160 or not run_id.isprintable():
        raise ValueError("Use a short printable run ID.")
    if not isinstance(record.get("result"), dict):
        raise ValueError("The Revit script result is unavailable.")
    return record


def require_no_changes(record, mode):
    decisions = ("no_changes", "rolled_back") if mode == "query" else ("no_changes",)
    record = require_run(record, mode, decisions)
    changes = record.get("changes")
    if (not isinstance(changes, dict)
            or any(type(changes.get(key)) is not int or changes[key] != 0
                   for key in ("added_count", "modified_count", "deleted_count"))
            or any(changes.get(key) for key in ("added", "modified", "deleted", "added_ids", "modified_ids",
                                                "deleted_ids", "by_category", "other_documents"))):
        raise ValueError("The Revit record reports unexpected model changes.")
    return record
