"""Verify a Toposolid alignment probe against independent Revit queries."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from toposolid_grade_check import finite

TRANSACTION = "Try reviewed Toposolid alignment and roll back"
LIMIT = 2 * 1024 * 1024


def digest(snapshot):
    data = json.dumps(snapshot, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def load_record(path, mode):
    with path.open("rb") as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("The Revit record exceeds 2 MiB.")
    raw = json.loads(data)
    record = raw.get("response", raw)
    run_id = record.get("run_id")
    decisions = ("rolled_back",) if mode == "query" else ("no_changes", "rolled_back")
    if (record.get("mode") != mode or record.get("status") != "ok" or record.get("decision") not in decisions
            or not isinstance(run_id, str) or not run_id.strip()
            or type(record.get("exit_code")) is not int or record["exit_code"] != 0
            or record.get("result_truncated") is not False or record.get("approval") is not None
            or any(record.get(field) for field in (
                "error", "warnings", "failures", "dialogs", "blocked", "closed_documents", "unclosed_documents",
                "reckless"))):
        raise ValueError("Use a complete successful Revit {} response.".format(mode))
    changes = record["changes"]
    allowed_transactions = ([],) if mode == "query" else ([], [TRANSACTION])
    if (changes.get("transactions") not in allowed_transactions or changes.get("by_category") != {}
            or changes.get("other_documents") or any(changes.get(kind + "_ids") for kind in ("added", "modified", "deleted"))
            or any(changes.get(kind) != [] or type(changes.get(kind + "_count")) is not int
                   or changes[kind + "_count"] != 0 for kind in ("added", "modified", "deleted"))):
        raise ValueError("The Revit record does not establish an empty net change set.")
    return run_id, record["result"]


def verify(before, trial, fresh):
    before_id, original = load_record(before, "query")
    trial_id, result = load_record(trial, "dry_run")
    fresh_id, restored = load_record(fresh, "query")
    run_ids = [before_id, trial_id, fresh_id]
    if len(set(run_ids)) != 3 or original.get("status") != "ok" or restored.get("status") != "ok":
        raise ValueError("Use three independent Revit runs with successful before and after queries.")
    baseline = json.loads(original["snapshot_json"])
    if json.loads(restored["snapshot_json"]) != baseline or result.get("baseline_sha256") != digest(baseline):
        raise ValueError("The probe and fresh query do not match the exact alignment baseline.")
    alignment = baseline["alignment"]
    rows = alignment["rows"]
    tolerance = alignment["tolerance_ft"]
    if (result.get("status") != "measured" or result.get("rollback_verified") is not True
            or result.get("document") != baseline["document"] or result.get("toposolid") != baseline["toposolid"]
            or result.get("support") != alignment["support"] or alignment["blocked"] is not False or not rows
            or not finite(tolerance) or tolerance <= 0 or len(result["vertices"]) != len(rows)):
        raise ValueError("The probe did not measure every reviewed alignment row.")
    for reviewed, observed in zip(rows, result["vertices"]):
        point, target = observed["position_ft"], reviewed["after_ft"]
        if (observed["point"] != reviewed["point"] or len(point) != 3
                or not all(finite(value) for value in point) or point[:2] != target[:2]):
            raise ValueError("A measured vertex differs from the reviewed alignment.")
        error = abs(point[2] - target[2])
        bound = error + reviewed["support_distance_ft"]
        if (error != observed["elevation_error_ft"] or bound != observed["support_distance_bound_ft"]
                or not 0 <= bound <= tolerance):
            raise ValueError("A measured vertex is outside the reviewed support tolerance.")
    return {"status": "toposolid_alignment_rollback_verified", "baseline_sha256": result["baseline_sha256"],
            "revit_build": result["revit_build"], "run_ids": run_ids, "vertices": result["vertices"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before", "trial", "fresh"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.before, args.trial, args.fresh), ensure_ascii=True, allow_nan=False))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
