"""Bind a reviewed route to a fresh disposable Revit fixture before a trial."""

import argparse
from hashlib import sha256
import importlib
import json
import math
import ntpath
from pathlib import Path
import sys

import mep_route_preview as route_preview
from view_duplicate_preview import element_id, read_payload, read_record as load_record, snapshot_digest
from view_filter_transfer_preview import MAX_SAVED_RECORD, checked_changes, checked_record


TRANSACTION_NAME = "Test reviewed MEP route"


def installed_reader():
    scripts = Path(route_preview.__file__).resolve().parents[2] / "am-issue-board" / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        reader = importlib.import_module("read_mep")
    finally:
        sys.path.pop(0)
    if (Path(reader.__file__).resolve() != scripts / "read_mep.py"
            or Path(reader.ib.__file__).resolve() != scripts / "issue_board.py"):
        raise ValueError("Use the matching installed issue-board scripts for trial preflight.")
    return reader


def read_record(reader, document, request, field):
    response = reader.run_query(document, request)
    if response.get("status") != "ok":
        raise ValueError(response.get("message") or "Revit could not reread the reviewed route evidence.")
    return json.loads(response[field])


def prepare(document, args, reader=None):
    if sys.implementation.name != "cpython" or sys.version_info < (3, 8):
        raise ValueError("Use the qualified CPython bridge for the route trial.")
    path = document.PathName
    if (args.get("disposable_fixture") is not True or document.IsWorkshared or document.IsReadOnly
            or document.IsModifiable or str(document.Application.VersionNumber) != "2026"
            or not path or not ntpath.isabs(path) or path.startswith(("\\", "//"))
            or not ntpath.splitdrive(path)[0] or ntpath.splitext(path)[1].lower() != ".rvt"):
        raise ValueError("Open the reviewed writable standalone Revit 2026 fixture.")
    raw = args["route_input_json"].encode("utf-8")
    if len(raw) > 256 * 1024:
        raise ValueError("The reviewed route input exceeds its size limit.")
    data = json.loads(raw, object_pairs_hook=route_preview.unique_object)
    reviewed_preview = args["preview"]
    computed = route_preview.preview(data)
    computed["basis_sha256"] = sha256(raw).hexdigest()
    if computed["blocked"] or json.loads(json.dumps(computed, allow_nan=False)) != reviewed_preview:
        raise ValueError("The saved review does not match a complete route preview.")

    route = computed["route"]
    source = route["source"]
    endpoints = route["endpoints"]
    points = route["points_ft"]
    segments = list(zip(points, points[1:]))
    if (source["link_instance_id"] is not None or source["host_identity"] != source["document_identity"]
            or len({row["element_id"] for row in endpoints}) != 2
            or any(row["shape"] != "Round" for row in endpoints)
            or not 1 <= len(segments) <= 25 or len(route["bends_degrees"]) != len(segments) - 1
            or any(math.dist(a, b) < 1 / 120 or (
                abs(a[2] - b[2]) > route_preview.EPSILON and
                math.hypot(a[0] - b[0], a[1] - b[1]) > route_preview.EPSILON) for a, b in segments)):
        raise ValueError("Use distinct round host endpoints and up to 25 level or vertical segments.")
    if ("routing_bounds_ft" not in data or data.get("obstacles")
            or (not route["obstacle_records"] and args.get("empty_obstacle_scope") is not True)):
        raise ValueError("Review the routing area and native obstacles or explicitly confirm an empty scope.")

    types_record = args["types_record"]
    owners_record = args["owners_record"]
    types = types_record["types"]
    fitting = types["fitting"]
    if (fitting["domain"] != endpoints[0]["domain"]
            or fitting["diameter_ft"] != endpoints[0]["dimensions_ft"][0]):
        raise ValueError("The reviewed curve type domain or diameter differs from the endpoints.")
    elbow = route["elbow_record"]
    if ((route["bends_degrees"] and (elbow is None or fitting["elbow"] is None))
            or (elbow is not None and elbow["fitting"] != fitting)):
        raise ValueError("The reviewed route and trial types disagree on elbow selection.")

    reader = reader or installed_reader()
    owner_inputs = [{key: row[key] for key in ("element_id", "element_uid", "connector_id")} for row in endpoints]
    owners_query = {"source": source, "route_trial_owners": owner_inputs}
    types_query = {"source": source, "route_trial_types": {
        "elbow": {key: fitting[key] for key in ("curve_type_id", "curve_type_uid", "diameter_ft")},
        "system_type": {key: types["system_type"][key] for key in ("id", "uid")}, "level": types["level"],
    }}
    obstacle_queries = []
    for record in route["obstacle_records"]:
        obstacle_source = record["source"]
        obstacle_queries.append((record, {
            "source": obstacle_source,
            "link_instance_id": obstacle_source["link_instance_id"],
            "link_instance_uid": obstacle_source["link_instance_uid"],
            "route_obstacles": [
                {key: row[key] for key in ("element_id", "element_uid")} for row in record["obstacles"]
            ],
        }))
    expected_owners = [(row["element_id"], row["element_uid"]) for row in endpoints]
    if (read_record(reader, document, owners_query, "trial_owners_json") != owners_record
            or owners_record["snapshot"]["endpoints"] != endpoints
            or [(row["element_id"], row["element_uid"]) for row in owners_record["snapshot"]["owners"]]
            != expected_owners
            or read_record(reader, document, types_query, "trial_types_json") != types_record
            or any(read_record(reader, document, query, "obstacles_json") != record
                   for record, query in obstacle_queries)):
        raise ValueError("The reviewed owners, types or obstacles differ from the current fixture.")

    return {
        "kind": "mep-route-trial-preflight", "version": 1, "source": source,
        "route_input_sha256": computed["basis_sha256"], "review_sha256": snapshot_digest(reviewed_preview),
        "owners_sha256": snapshot_digest(owners_record), "types_sha256": snapshot_digest(types_record),
        "owner_ids": sorted((row["element_id"] for row in endpoints), key=int),
    }


def expected_bridge_changes(changes, rollback_events):
    """Apply the inspected pyRevit change recorder's set operations to the native events."""
    if not isinstance(rollback_events, list) or len(rollback_events) > 8:
        raise ValueError("The rollback event inventory is incomplete or outside its bound.")
    added = set(changes["added"])
    original_modified = set(changes["modified"])
    modified = set(original_modified)
    for event in rollback_events:
        if (set(event) != {"operation", "transactions", "added", "modified", "deleted"}
                or event["operation"] not in ("TransactionRolledBack", "TransactionGroupRolledBack")
                or event["transactions"] not in ([], [TRANSACTION_NAME]) or event["added"] != []):
            raise ValueError("The route rollback reported an unexpected event.")
        removed, touched = set(event["deleted"]), set(event["modified"])
        if (event["deleted"] != sorted(removed, key=int) or event["modified"] != sorted(touched, key=int)
                or not removed.issubset(added) or touched & removed
                or not touched.issubset((added - removed) | original_modified)):
            raise ValueError("The route rollback event reported unexpected element identities.")
        added.difference_update(removed)
        modified.difference_update(removed)
        modified.update(touched - added)
    return {"added": sorted(added, key=int), "modified": sorted(modified, key=int), "deleted": []}


def verify_trial(original_record, trial_record, fresh_record):
    original = checked_record(original_record, "query")
    trial = checked_record(trial_record, "dry_run")
    fresh = checked_record(fresh_record, "query")
    if len({record["run_id"] for record in (original, trial, fresh)}) != 3:
        raise ValueError("Use three independent pyRevit run records.")
    checked_changes(original, [])
    checked_changes(fresh, [])
    baseline = read_payload(original, "preflight_json")
    if read_payload(fresh, "preflight_json") != baseline:
        raise ValueError("The fresh route baseline differs after rollback.")
    result = trial["result"]
    if result["status"] != "mep_route_trial_rolled_back":
        raise ValueError("The native trial did not report a clean route rollback.")
    receipt = result["receipt"]
    if receipt["preflight"] != baseline:
        raise ValueError("The native trial used a different reviewed route.")
    changes = receipt["changes"]
    observed = [element_id(item["id"]) for item in receipt["measurements"]]
    expected = sorted(set(observed), key=int)
    if (not 1 <= len(observed) <= 49 or len(expected) != len(observed) or changes["added"] != expected
            or changes["modified"] != baseline["owner_ids"] or changes["deleted"]
            or fresh["result"].get("confirmed_absent_ids") != expected):
        raise ValueError("The complete change list and independent absence check disagree.")
    bridge = expected_bridge_changes(changes, receipt["rollback_events"])
    checked_changes(trial, bridge["modified"], bridge["added"], TRANSACTION_NAME)
    return {"status": "verified_mep_route_trial_rollback", "preflight": baseline, "changes": changes,
            "bridge_changes": bridge,
            "original_run_id": original["run_id"], "trial_run_id": trial["run_id"], "fresh_run_id": fresh["run_id"]}


def query(document, args):
    reader = installed_reader()
    prepared = prepare(document, args, reader)
    result = {"status": "ok", "preflight_json": json.dumps(prepared, ensure_ascii=True)}
    if "check_absent_ids" in args:
        ids = args["check_absent_ids"]
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 49
                or ids != sorted({element_id(item) for item in ids}, key=int)):
            raise ValueError("Use the trial's exact sorted addition IDs for the absence check.")
        if any(document.GetElement(reader.element_id(item)) is not None for item in ids):
            raise ValueError("Revit still resolves a trial addition.")
        result["confirmed_absent_ids"] = ids
    return result


def main():
    parser = argparse.ArgumentParser(description="Verify a Revit MEP route rollback from three saved records.")
    parser.add_argument("records", type=Path, nargs=3, metavar="RECORD")
    args = parser.parse_args()
    try:
        result = verify_trial(*(load_record(path, MAX_SAVED_RECORD) for path in args.records))
    except (AttributeError, KeyError, TypeError, ValueError, OSError) as error:
        print(f"MEP route verification stopped. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if "inputs" in globals():
    try:
        result = query(doc, inputs)
    except (AttributeError, KeyError, TypeError, ValueError, OSError, ImportError, RecursionError) as error:
        result = {"status": "stopped", "message": str(error)}
elif __name__ == "__main__":
    sys.exit(main())
