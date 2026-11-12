"""Preview explicit text fills from a complete read-only Revit MEP query."""

import argparse
import json
import re
import sys
from uuid import UUID

from view_duplicate_preview import element_id, read_payload, read_record, snapshot_digest
from view_filter_transfer_preview import MAX_SAVED_RECORD, checked_changes, checked_record


TRANSACTION_NAME = "Test reviewed MEP text fill"
COLUMNS = ["ElementId", "Instance UID", "Category ID", "Parameter ID", "Current state", "Text"]
NAME_COLUMNS = ["ElementId", "UID", "Role", "Connector assignments", "System name"]
BLOCKED = {
    "missing_parameter": "The shared parameter is missing.",
    "unsupported_storage": "The shared parameter does not store text.",
    "read_only": "The shared parameter is read-only.",
}


def parameter_state(row):
    if "parameter_id" not in row:
        if set(row) != {"id", "uid", "category_id", "state"}:
            raise ValueError("A missing parameter row contains contradictory evidence.")
        return "missing_parameter"
    element_id(row["parameter_id"])
    value = row["value"]
    if (type(row["has_value"]) is not bool or type(row["read_only"]) is not bool
            or (value is not None and (not isinstance(value, str) or len(value) > 4096))):
        raise ValueError("The stored parameter evidence is invalid.")
    if row["storage"] != "String":
        if value is not None:
            raise ValueError("A nontext parameter contains unexpected text evidence.")
        return "unsupported_storage"
    if row["read_only"]:
        return "read_only"
    if value and value.strip():
        return "already_filled"
    return "empty" if row["has_value"] else "unset"


def query_evidence(saved, field, kind):
    record = checked_record(saved, "query")
    checked_changes(record, [])
    if record.get("closed_documents"):
        raise ValueError("The query closed a document. Rerun the read-only repair query.")
    evidence = read_payload(record, field)
    if evidence["kind"] != kind or type(evidence["version"]) is not int or evidence["version"] != 1:
        raise ValueError("Use the native MEP repair evidence query.")
    source = evidence["source"]
    host = source["host_identity"]
    if (source["document_identity"] != host or source["link_instance_id"] is not None
            or source["link_instance_uid"] is not None or not isinstance(host["project_uid"], str)
            or not host["project_uid"] or not re.fullmatch(r"[0-9a-f]{64}", host["path_sha256"])):
        raise ValueError("Use a verified standalone host project.")
    return record, evidence


def preview(saved, request):
    if not isinstance(request, dict) or set(request) != {"text"}:
        raise ValueError("Supply one explicit replacement text value.")
    text = request["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise ValueError("Supply nonblank replacement text of up to 4096 characters.")
    record, evidence = query_evidence(saved, "repair_parameters_json", "mep-parameter-repair-evidence")
    guid = UUID(evidence["guid"])
    if not guid.int or str(guid) != evidence["guid"]:
        raise ValueError("The shared parameter GUID is invalid.")
    rows = evidence["elements"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        raise ValueError("Use evidence for one to 50 exact instances.")

    before, after, warnings, targets = [], [], [], []
    seen_ids, seen_uids = set(), set()
    for row in rows:
        identifier, uid = element_id(row["id"]), row["uid"]
        if (identifier in seen_ids or not isinstance(uid, str) or not uid or len(uid) > 160
                or uid in seen_uids or not re.fullmatch(r"-[1-9][0-9]{0,9}", row["category_id"])):
            raise ValueError("Use distinct and exact MEP instance identities.")
        seen_ids.add(identifier)
        seen_uids.add(uid)
        state = parameter_state(row)
        if state != row["state"]:
            raise ValueError("A repair row contradicts the stored parameter evidence.")
        previous = row.get("value")
        eligible = state in ("empty", "unset")
        fields = [identifier, uid, row["category_id"], row.get("parameter_id"), state]
        before.append(fields + [previous])
        after.append(fields + [text if eligible else previous])
        if eligible:
            targets.append(identifier)
        if state in BLOCKED:
            warnings.append({"row": identifier, "text": BLOCKED[state], "blocking": True})
    if not targets and not warnings:
        warnings.append({"text": "No selected instance has a blank writable text parameter.", "blocking": True})

    basis = snapshot_digest({"run_id": record["run_id"], "evidence": evidence, "text": text})
    return {
        "kind": "change-preview", "recipe": "mep-parameter-repair", "title": "Fill blank MEP text parameters",
        "source_run_id": record["run_id"], "basis_sha256": basis,
        "keyColumn": COLUMNS[0], "columns": COLUMNS, "changedColumns": ["Text"],
        "notes": ["Shared parameter GUID " + str(guid)], "before": before, "after": after,
        "warnings": warnings, "blocked": bool(warnings), "review_only": True,
        "steps": [], "ownership_ids": targets,
    }


def system_identity(row):
    uid = row["uid"]
    if not isinstance(uid, str) or not uid or len(uid) > 160:
        raise ValueError("The system or member UID is unavailable.")
    return {"id": element_id(row["id"]), "uid": uid}


def preview_system_name(saved):
    record, evidence = query_evidence(saved, "repair_system_name_json", "mep-system-name-repair-evidence")
    system = evidence["system"]
    identity = system_identity(system)
    system_identity(system["system_type"])
    old, new = system["name"], evidence["proposed_name"]
    if (any(not isinstance(name, str) or not name.strip() or len(name) > 160 for name in (old, new))
            or not isinstance(system["classification"], str) or not system["classification"]
            or new != new.strip() or any(ord(char) < 32 or ord(char) == 127 for char in new)):
        raise ValueError("The current name, proposed name or classification is invalid.")
    roles, members = system["roles"], system["members"]
    if set(roles) != {"network", "terminals", "base_equipment"} or not isinstance(members, list) or len(members) > 500:
        raise ValueError("The reviewed system membership is incomplete.")
    role_ids = {}
    for role, ids in roles.items():
        if not isinstance(ids, list) or len(ids) != len(set(ids)):
            raise ValueError("The system role list is invalid.")
        role_ids[role] = {element_id(item) for item in ids}
    if sum(map(len, role_ids.values())) > 500 or len(role_ids["base_equipment"]) > 1:
        raise ValueError("The system role list exceeds its limit.")

    rows, member_ids, member_uids = [], set(), set()
    for member in members:
        target = system_identity(member)
        identifier, uid = target["id"], target["uid"]
        if identifier == identity["id"] or identifier in member_ids or uid in member_uids:
            raise ValueError("The system membership has duplicate identities.")
        member_ids.add(identifier)
        member_uids.add(uid)
        assignments = member["assignments"]
        if not isinstance(assignments, list) or not 1 <= len(assignments) <= 32:
            raise ValueError("The member connector evidence is incomplete.")
        connector_ids, has_system = set(), False
        for assignment in assignments:
            connector_id, assigned = assignment["connector_id"], assignment["system"]
            if type(connector_id) is not int or connector_id in connector_ids:
                raise ValueError("A member has invalid connector identities.")
            connector_ids.add(connector_id)
            if assigned is not None and system_identity(assigned) == identity:
                has_system = True
        if not has_system:
            raise ValueError("A member has no matching connector assignment.")
        role = ", ".join(key for key, ids in role_ids.items() if identifier in ids)
        rows.append([identifier, uid, role, json.dumps(assignments, ensure_ascii=True, sort_keys=True), None])
    if set().union(*role_ids.values()) != member_ids:
        raise ValueError("The system roles differ from the member identities.")

    warnings = []
    conflicts = evidence["name_conflicts"]
    if not isinstance(conflicts, list) or len(conflicts) > 5000:
        raise ValueError("The system name collision evidence is invalid.")
    for conflict in conflicts:
        peer = system_identity(conflict)
        if peer["id"] == identity["id"] or conflict["name"].upper() != new.upper():
            raise ValueError("The system name collision evidence contradicts the proposed name.")
        warnings.append({"row": identity["id"], "text": f"Name already used by system {peer['id']}.", "blocking": True})
    if old == new:
        warnings.append({"row": identity["id"], "text": "The system already has this name.", "blocking": True})

    row = [identity["id"], identity["uid"], "system", None]
    return {
        "kind": "change-preview", "recipe": "mep-system-name", "title": "Review MEP system rename",
        "source_run_id": record["run_id"],
        "basis_sha256": snapshot_digest({"run_id": record["run_id"], "evidence": evidence}),
        "keyColumn": NAME_COLUMNS[0], "columns": NAME_COLUMNS, "changedColumns": ["System name"],
        "notes": ["System classification " + system["classification"]],
        "before": [row + [old], *rows], "after": [row + [new], *rows],
        "warnings": warnings, "blocked": bool(warnings), "review_only": True,
        "steps": [], "ownership_ids": sorted(member_ids | {identity["id"]}, key=int),
    }


def verify_trial(original_saved, fill, trial_saved, fresh_saved):
    display = preview(original_saved, fill)
    original = checked_record(original_saved, "query")
    trial = checked_record(trial_saved, "dry_run")
    fresh = checked_record(fresh_saved, "query")
    if len({original["run_id"], trial["run_id"], fresh["run_id"]}) != 3:
        raise ValueError("Supply three independent Revit run records.")
    if trial.get("closed_documents") or fresh.get("closed_documents"):
        raise ValueError("The trial or readback closed another document.")
    modified = sorted(display["ownership_ids"], key=int)
    result = trial["result"]
    if (display["blocked"] or result["status"] != "mep_text_fill_trial_rolled_back"
            or result["basis_sha256"] != display["basis_sha256"]
            or result["source_run_id"] != original["run_id"]
            or result["trial_changes"] != {"added": [], "modified": modified, "deleted": []}):
        raise ValueError("The text-fill trial does not match the reviewed preview and effects.")
    checked_changes(trial, modified, transaction_name=TRANSACTION_NAME)
    checked_changes(fresh, [])
    if read_payload(fresh, "repair_parameters_json") != read_payload(original, "repair_parameters_json"):
        raise ValueError("The independent Revit readback differs from the original parameters.")
    return {
        "status": "verified_mep_text_fill_trial_rollback", "basis_sha256": display["basis_sha256"],
        "original_run_id": original["run_id"], "trial_run_id": trial["run_id"], "fresh_run_id": fresh["run_id"],
        "modified_ids": modified,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="Complete pyRevit response or result pages from one query")
    parser.add_argument("fill", nargs="?", help='JSON file with the approved proposal, for example {"text": "Ventilation"}')
    parser.add_argument("--system-name", action="store_true", help="Preview a reviewed MEP system name")
    parser.add_argument("--trial", help="Complete dry-run response from the disposable text-fill trial")
    parser.add_argument("--fresh", help="Complete response from a separate post-trial read-only query")
    args = parser.parse_args()
    try:
        original = read_record(args.query, MAX_SAVED_RECORD)
        if args.system_name:
            if args.fill or args.trial or args.fresh:
                raise ValueError("Use only the reviewed system-name query with --system-name.")
            result = preview_system_name(original)
        else:
            if not args.fill or bool(args.trial) != bool(args.fresh):
                raise ValueError("Supply the fill request and, for verification, both saved trial and readback.")
            fill = read_record(args.fill, 65536)
            if args.trial:
                result = verify_trial(original, fill, read_record(args.trial, MAX_SAVED_RECORD),
                                      read_record(args.fresh, MAX_SAVED_RECORD))
            else:
                result = preview(original, fill)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        print(f"MEP repair preview stopped. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return int(result.get("blocked", False))


if __name__ == "__main__":
    sys.exit(main())
