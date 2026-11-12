"""Verify a level-alignment rollback from three independent saved Revit run records."""

import argparse
import json
import re
import sys

from level_alignment_preview import (
    MAX_INPUT, POSITION_TOLERANCE, ROTATION_TOLERANCE, TRANSACTION_NAME, element_id, finite, probe_plan, rotation_delta,
)

MAX_RECORD = 4 * MAX_INPUT


def runtime_record(value, mode):
    record = value.get("response", value)
    engine = record.get("engine")
    run_id = record.get("run_id")
    if (record.get("mode") != mode or record.get("status") != "ok"
            or record.get("decision") not in ("no_changes", "rolled_back")
            or type(record.get("exit_code")) is not int or record["exit_code"] != 0
            or record.get("result_truncated") is not False or record.get("error") is not None
            or record.get("approval") is not None or record.get("unclosed_documents", [])
            or any(record.get(field) != [] for field in ("blocked", "dialogs", "failures", "warnings"))
            or not isinstance(engine, dict) or engine.get("requested") != "cpython"
            or not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{12}", run_id)
            or not isinstance(record.get("result"), dict)):
        raise ValueError("Use complete successful CPython Revit records with no approval or warnings.")
    return record


def runtime_changes(record, expected=(), transaction=None):
    changes = record["changes"]
    for kind in ("added", "modified", "deleted"):
        count = changes.get(kind + "_count")
        rows = changes.get(kind, [])
        if (type(count) is not int or not isinstance(rows, list) or len(rows) != count
                or changes.get(kind + "_ids")):
            raise ValueError("Revit effect counts and full element lists disagree.")
        if kind != "modified" and rows:
            raise ValueError("Revit reported an addition or deletion outside the reviewed operation.")
    if changes.get("other_documents"):
        raise ValueError("Revit reported an effect in another document.")
    modified = [str(row["id"]) for row in changes.get("modified", [])]
    if modified:
        if (record["decision"] != "rolled_back" or not all(map(element_id, modified))
                or sorted(modified, key=int) != list(expected)):
            raise ValueError("Revit did not report the exact reviewed modified element IDs.")
    transactions = changes.get("transactions", [])
    if transactions != ([transaction] if modified else []) and not (
            not modified and transaction and record["decision"] == "rolled_back" and transactions == [transaction]):
        raise ValueError("Revit reported transactions outside the reviewed probe.")
    categories = changes.get("by_category")
    if (not isinstance(categories, dict) or any(type(count) is not int or count <= 0 for count in categories.values())
            or sum(categories.values()) != len(modified)):
        raise ValueError("Revit category totals disagree with the modified element list.")


def snapshot(record):
    result = record["result"]
    raw = result.get("snapshot_json")
    if result.get("status") != "ok" or not isinstance(raw, str) or not 0 < len(raw) <= MAX_INPUT:
        raise ValueError("The Revit query did not return a complete alignment snapshot.")
    return json.loads(raw)


def verify_measurements(expected, observed):
    if not isinstance(observed, dict) or expected.keys() != observed.keys():
        raise ValueError("The trial did not measure every reviewed target.")
    for id_value, wanted in expected.items():
        row = observed[id_value]
        point, rotation, offset = row.get("point"), row.get("rotation_rad"), row.get("offset_ft")
        if (row.keys() != wanted.keys() or not isinstance(point, list) or len(point) != 3
                or any(not finite(actual) or abs(actual - target) > POSITION_TOLERANCE
                       for actual, target in zip(point, wanted["point"]))
                or not finite(rotation) or abs(rotation_delta(rotation, wanted["rotation_rad"])) > ROTATION_TOLERANCE
                or row.get("level_uid") != wanted["level_uid"]):
            raise ValueError("The measured position, rotation, or level differs from the approved change.")
        target_offset = wanted["offset_ft"]
        if target_offset is None:
            if offset is not None:
                raise ValueError("The trial reported an offset outside the reviewed scope.")
        elif not finite(offset) or abs(offset - target_offset) > POSITION_TOLERANCE:
            raise ValueError("The measured offset differs from the approved change.")


def verify(plan, original_record, trial_record, fresh_record):
    original = runtime_record(original_record, "query")
    trial = runtime_record(trial_record, "dry_run")
    fresh = runtime_record(fresh_record, "query")
    run_ids = [record["run_id"] for record in (original, trial, fresh)]
    if len(set(run_ids)) != 3:
        raise ValueError("Use three independent Revit run records.")

    runtime_changes(original)
    runtime_changes(fresh)
    baseline = snapshot(original)
    if probe_plan(baseline) != plan or snapshot(fresh) != baseline:
        raise ValueError("The approved plan, original snapshot, and fresh rollback snapshot disagree.")
    revit = baseline["revit"]
    if (not isinstance(revit, dict) or set(revit) != {"version", "build"}
            or not all(isinstance(value, str) and value.strip() for value in revit.values())):
        raise ValueError("The snapshot does not identify the Revit version and build.")

    result = trial["result"]
    transaction = result.get("transaction")
    expected = sorted(plan["changes"], key=int)
    if (result.get("status") != "trial_rolled_back" or result.get("rollback_verified") is not True
            or result.get("revit") != revit or result.get("basis_sha256") != plan["basis_sha256"]
            or result.get("modified_ids") != expected or not isinstance(transaction, str)
            or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [a-f0-9]{32}", transaction)):
        raise ValueError("The native trial does not prove the exact reviewed edit and rollback.")

    events = result.get("rollback_events")
    if not isinstance(events, list) or len(events) > 8 or any(
            event.get("operation") not in ("TransactionRolledBack", "TransactionGroupRolledBack")
            or not isinstance(event.get("modified"), list)
            or any(item not in expected for item in event["modified"]) for event in events):
        raise ValueError("The rollback event list includes unreviewed effects.")
    verify_measurements(plan["changes"], result["measurements"])
    runtime_changes(trial, expected, transaction)
    return {
        "status": "level_alignment_rollback_verified",
        "basis_sha256": plan["basis_sha256"],
        "revit": revit,
        "modified_ids": expected,
        "original_run_id": run_ids[0],
        "trial_run_id": run_ids[1],
        "fresh_run_id": run_ids[2],
    }


def load(path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_RECORD + 1)
    if len(raw) > MAX_RECORD:
        raise ValueError("The saved record exceeds 4 MiB.")
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "original", "trial", "fresh"):
        parser.add_argument(name)
    args = parser.parse_args()
    try:
        result = verify(*(load(path) for path in (args.plan, args.original, args.trial, args.fresh)))
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
