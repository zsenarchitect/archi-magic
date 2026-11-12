"""Verify a plan dimension rollback from three independent saved Revit records."""

import argparse
import json
import re
import sys

from plan_dimension_preview import (
    MAX_INPUT, TRANSACTION_NAME, compare, element_id, existing_signature,
    geometry_context, label, probe_plan, proposal_signature,
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


def effect_ids(changes, kind):
    rows, count = changes.get(kind, []), changes.get(kind + "_count")
    if (type(count) is not int or not isinstance(rows, list) or len(rows) != count
            or count > 20 or changes.get(kind + "_ids")):
        raise ValueError("Revit effect counts and full element lists disagree.")
    ids = set()
    for row in rows:
        value = row if kind == "deleted" else row["id"]
        value = element_id(str(value) if type(value) is int else value)
        ids.add(value)
    if len(ids) != count:
        raise ValueError("The Revit effect list contains repeated IDs.")
    return ids


def runtime_effects(record, expected=(), transaction=None):
    changes = record["changes"]
    added, modified, deleted = (effect_ids(changes, kind) for kind in ("added", "modified", "deleted"))
    if modified or changes.get("other_documents"):
        raise ValueError("Revit reported a modification or an effect in another document.")
    if added or deleted:
        if record["decision"] != "rolled_back" or deleted != set(expected) or not added.issubset(deleted):
            raise ValueError("Revit effects do not match the temporary dimension IDs and their removal.")
    transactions = changes.get("transactions", [])
    if added or deleted:
        if transactions != [transaction]:
            raise ValueError("Revit reported transactions outside the reviewed dimension probe.")
    elif transactions and (record["decision"] != "rolled_back" or not transaction or transactions != [transaction]):
        raise ValueError("Revit reported transactions outside the reviewed dimension probe.")
    categories = changes.get("by_category")
    if (not isinstance(categories, dict) or any(type(count) is not int or count <= 0 for count in categories.values())
            or sum(categories.values()) != len(added)):
        raise ValueError("Revit category totals disagree with the temporary additions.")


def snapshot(record):
    result = record["result"]
    raw = result.get("snapshot_json")
    if result.get("status") != "ok" or not isinstance(raw, str) or not 0 < len(raw) <= MAX_INPUT:
        raise ValueError("The Revit query did not return a complete dimension snapshot.")
    return json.loads(raw)


def verify_measurements(plan, measurements, baseline):
    proposals = plan["proposals"]
    if not isinstance(measurements, dict) or measurements.keys() != proposals.keys():
        raise ValueError("The trial did not measure every reviewed dimension string.")
    context = geometry_context(baseline)
    ids, uids = set(), set()
    for name, proposal in proposals.items():
        measurement = measurements[name]
        if measurement.get("owner_view_id") != baseline["view"]["id"]:
            raise ValueError("A measured dimension belongs to a different view.")
        measured = existing_signature(measurement, context)
        expected = proposal_signature(proposal, context)
        if compare(expected, measured) != "match":
            raise ValueError("A native dimension line, type, reference, or segment value differs from the plan.")
        ids.add(element_id(measurement["id"]))
        uids.add(label(measurement["uid"]))
    if len(ids) != len(proposals) or len(uids) != len(proposals) or ids.intersection(
            row["id"] for row in baseline["existing"]):
        raise ValueError("The native trial did not report distinct dimension identities.")
    return sorted(ids, key=int)


def verify(plan, original_record, trial_record, fresh_record):
    original = runtime_record(original_record, "query")
    trial = runtime_record(trial_record, "dry_run")
    fresh = runtime_record(fresh_record, "query")
    run_ids = [record["run_id"] for record in (original, trial, fresh)]
    if len(set(run_ids)) != 3:
        raise ValueError("Use three independent Revit run records.")

    runtime_effects(original)
    runtime_effects(fresh)
    baseline = snapshot(original)
    if probe_plan(baseline) != plan or snapshot(fresh) != baseline:
        raise ValueError("The approved plan, original snapshot, and fresh rollback snapshot disagree.")
    revit = baseline["revit"]
    if (not isinstance(revit, dict) or set(revit) != {"version", "build"}
            or not all(isinstance(value, str) and value.strip() for value in revit.values())):
        raise ValueError("The snapshot does not identify the Revit version and build.")

    result = trial["result"]
    transaction = result.get("transaction")
    if (result.get("status") != "trial_rolled_back" or result.get("rollback_verified") is not True
            or result.get("revit") != revit or result.get("basis_sha256") != plan["basis_sha256"]
            or not isinstance(transaction, str)
            or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [a-f0-9]{32}", transaction)):
        raise ValueError("The native trial does not prove rollback of the approved dimension plan.")

    expected = verify_measurements(plan, result["measurements"], baseline)
    if (result.get("temporary_dimension_ids") != expected
            or fresh["result"].get("confirmed_absent_ids") != expected):
        raise ValueError("The fresh query did not confirm absence of every measured temporary dimension.")
    runtime_effects(trial, expected, transaction)
    return {
        "status": "plan_dimension_rollback_verified",
        "basis_sha256": plan["basis_sha256"],
        "revit": revit,
        "temporary_dimension_ids": expected,
        "original_run_id": run_ids[0],
        "trial_run_id": run_ids[1],
        "fresh_run_id": run_ids[2],
    }


def load(path):
    with open(path, "rb") as stream:
        raw = stream.read(MAX_RECORD + 1)
    if len(raw) > MAX_RECORD:
        raise ValueError("A saved record exceeds 8 MiB.")
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
