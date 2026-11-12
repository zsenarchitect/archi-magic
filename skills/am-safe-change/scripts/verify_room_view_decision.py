"""Verify an explicitly approved Revit room-view replay and fresh readback."""

import argparse
import json
from pathlib import Path
import re
import sys

import verify_room_view_trial as trial_check


REPLAY = re.compile(r"Archi Magic room view replay [0-9a-f]{32}")


def measured_views_match(first, second, keep_identities):
    fields = {"view_origin", "direction", "crop", "crop_world", "far_extent", "marker_point"}
    excluded = set() if keep_identities else {"view", "marker"}
    if len(first) != len(second):
        raise ValueError("The replay did not measure the complete reviewed view set.")
    for a, b in zip(first, second):
        if set(a) != set(b):
            raise ValueError("The measured view or marker fields differ.")
        for key in a:
            if key in excluded:
                continue
            if key == "far_extent":
                same = abs(a[key] - b[key]) <= trial_check.TOLERANCE
            elif key in ("crop", "crop_world"):
                same = len(a[key]) == len(b[key]) and all(
                    trial_check.close(left, right) for left, right in zip(a[key], b[key]))
            elif key in fields:
                same = trial_check.close(a[key], b[key])
            else:
                same = a[key] == b[key]
            if not same:
                raise ValueError("The measured room view differs in %s." % key)


def native_decision(record, replay, ids):
    decision = record.get("decision")
    expected_status = {"committed": "ok", "discarded": "rejected"}.get(decision)
    if (expected_status is None or record.get("status") != expected_status or record.get("mode") != "modify"
            or record.get("approval") != "user" or record.get("result_truncated") is not False
            or type(record.get("exit_code")) is not int or record["exit_code"] != 0
            or not trial_check.planner.text(record.get("run_id")) or record.get("error") is not None
            or any(record.get(key) for key in ("failures", "dialogs", "warnings", "blocked", "unclosed_documents"))):
        raise ValueError("Use a complete native user Keep or Discard response.")
    changes = record["changes"]
    if (changes.get("other_documents") or changes.get("transactions") != [replay["transaction"]]
            or any(changes.get(key + "_ids") for key in trial_check.EFFECTS)
            or any(type(changes.get(key + "_count")) is not int or changes[key + "_count"] != 0
                   or changes.get(key) != [] for key in ("modified", "deleted"))):
        raise ValueError("The bridge reports effects outside the reviewed room view additions.")
    rows = changes["added"]
    if not isinstance(rows, list) or len(rows) != len(ids):
        raise ValueError("The bridge did not report every addition before the user decision.")
    seen = set()
    for row in rows:
        value = row["id"]
        identifier = str(value) if type(value) is int else value
        if identifier in seen:
            raise ValueError("The bridge addition list repeats an ID.")
        seen.add(identifier)
    counts = changes.get("by_category")
    if (seen != set(ids) or type(changes.get("added_count")) is not int or changes["added_count"] != len(ids)
            or not isinstance(counts, dict) or any(type(count) is not int or count <= 0 for count in counts.values())
            or sum(counts.values()) != len(ids)):
        raise ValueError("The bridge additions differ from the native replay.")
    return decision


def verify(before, dry_run, rollback, modify, fresh, approved_digest):
    checked = trial_check.verify(before, dry_run, rollback, approved_digest)
    baseline = before.get("response", before)["result"]
    trial = trial_check.decode(dry_run.get("response", dry_run)["result"]["trial_json"])
    bridge = modify.get("response", modify)
    native = bridge["result"]
    replay = trial_check.decode(native["replay_json"])
    name = replay["transaction"]
    if (native.get("status") != "awaiting_bridge_decision" or replay.get("status") != "awaiting_bridge_decision"
            or not isinstance(name, str) or not REPLAY.fullmatch(name)
            or any(replay.get(key) != trial[key] for key in (
                "proposal_sha256", "baseline_audit", "fixture_settings"))):
        raise ValueError("The replay does not use the same reviewed evidence and fixture settings.")
    snapshot = trial_check.decode(baseline["snapshot_json"])
    preview = trial_check.planner.compute(snapshot)
    additions = trial_check.measured_additions(replay, preview, snapshot)
    ids = [item["id"] for item in additions]
    if replay["effects"] != [{"operation": "TransactionCommitted", "added": ids, "modified": [], "deleted": []}]:
        raise ValueError("The replay's native transaction effects differ from the reviewed additions.")
    measured_views_match(trial["views"], replay["views"], keep_identities=False)
    decision = native_decision(bridge, replay, ids)
    query = trial_check.run_record(fresh, "query", [])
    if len(set(checked["run_ids"] + [bridge["run_id"], query["run_id"]])) != 5:
        raise ValueError("The replay and its fresh query must be independent of the trial runs.")
    result = query["result"]
    if decision == "committed":
        persisted = trial_check.decode(result["kept_json"])
        if (result.get("status") != "kept_room_views_read" or persisted.get("status") != "kept_room_views_read"
                or persisted.get("proposal_sha256") != approved_digest
                or persisted.get("fixture_settings") != trial["fixture_settings"]
                or persisted.get("baseline_inventory_sha256") != trial["baseline_audit"]["inventory_sha256"]
                or not trial_check.planner.SHA256.fullmatch(persisted.get("persisted_inventory_sha256", ""))
                or not trial_check.TOKEN.fullmatch(persisted["audit_token"])
                or persisted["audit_token"] in (baseline["audit_token"],
                                                rollback.get("response", rollback)["result"]["audit_token"])
                or trial_check.measured_additions(persisted, preview, snapshot) != additions):
            raise ValueError("The fresh Keep query does not prove all replay additions persisted.")
        measured_views_match(replay["views"], persisted["views"], keep_identities=True)
    elif (result.get("status") != "ok" or result.get("trial_transaction") != name
          or not trial_check.TOKEN.fullmatch(result["audit_token"])
          or result["audit_token"] in (baseline["audit_token"],
                                       rollback.get("response", rollback)["result"]["audit_token"])
          or any(result[key] != baseline[key] for key in ("snapshot_json", "inventory_json"))
          or trial_check.exact_additions(result["confirmed_absent"]) != additions):
        raise ValueError("The fresh Discard query does not prove the reviewed model was restored.")
    return {"status": "verified_room_view_" + decision, "transaction": name,
            "proposal_sha256": approved_digest, "replay_run_id": bridge["run_id"],
            "readback_run_id": query["run_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    stages = ("before", "trial", "rollback", "modify", "fresh")
    for stage in stages:
        parser.add_argument("--" + stage, required=True, type=Path)
    parser.add_argument("--approved-digest", required=True)
    args = parser.parse_args()
    try:
        records = [trial_check.read(getattr(args, stage)) for stage in stages]
        print(json.dumps(verify(*records, args.approved_digest), ensure_ascii=True))
    except (AttributeError, KeyError, OSError, TypeError, ValueError, RecursionError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
