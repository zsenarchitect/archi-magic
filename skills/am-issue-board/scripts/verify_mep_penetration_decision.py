"""Verify a native opening placement Keep or Discard against a fresh read."""

import argparse
import json
from pathlib import Path
import re
import sys

from mep_penetration_preview import fingerprint, preview
from probe_mep_penetration_placement import TRANSACTION_NAME, placement_plan
from structural_compare_cli import digest, read_input
from verify_mep_penetration_trial import audit, bridge_effects, exact_ids, measured, require_run, verify


def match_replay(proof, rows, effects):
    prior = proof["measurements"]
    keys = ("crossing_key", "opening_sha256", "cut_outline", "removed_volume_ft3", "activated_symbol")
    if (len(rows) != len(prior)
            or any(any(a[key] != b[key] for key in keys) for a, b in zip(prior, rows))):
        raise ValueError("The replay geometry or symbol activation differs from the verified trial.")
    old, new = proof["effects"], effects
    if (old["deleted"] or new["deleted"]
            or set(new["added"]) != {row["opening_id"] for row in rows}
            or set(old["modified"]) - set(old["added"]) != set(new["modified"]) - set(new["added"])):
        raise ValueError("The replay changed elements outside the verified placement effects.")


def verify_kept(snapshot, fresh, result):
    if any(snapshot[key] != fresh[key] for key in ("host_identity", "link_identity", "link_instance_uid", "link_transform")
           if key in snapshot or key in fresh):
        raise ValueError("The kept audit refers to a different model or link.")
    report = preview(fresh)
    if not report["complete"]:
        raise ValueError("The kept project has incomplete tracking or geometry evidence.")
    states = {row["crossing_key"]: row["status"] for row in report["findings"]}
    for row in result["rows"]:
        key = row["crossing_key"]
        current = [item for item in fresh["tracked"] if item["crossing_key"] == key]
        if (states.get(key) != "current" or len(current) != 1
                or current[0]["observed_uid"] != row["opening_uid"]
                or current[0].get("observed_id") != row["opening_id"]
                or current[0]["observed_opening_sha256"] != row["opening_sha256"]
                or current[0]["tracking_sha256"] != row["tracking_sha256"]):
            raise ValueError("A kept opening or its cut differs from the approved replay.")


def verify_decision(choices, before_raw, trial_raw, after_raw, modify_raw, fresh_raw):
    proof = verify(choices, before_raw, trial_raw, after_raw)
    before = require_run(before_raw, "query", ("no_changes", "rolled_back"))
    snapshot, inventory, before_token, _ = audit(before)
    plan = placement_plan(snapshot, choices)
    after = require_run(after_raw, "query", ("no_changes", "rolled_back"))
    after_token = after["result"]["audit_token"]
    decision = modify_raw.get("response", modify_raw).get("decision")
    if decision not in {"committed", "discarded"}:
        raise ValueError("Use the full native Keep or Discard run.")
    kept = decision == "committed"
    modify = require_run(modify_raw, "modify", (decision,), approval="user", status="ok" if kept else "rejected")
    fresh_run = require_run(fresh_raw, "query", ("no_changes", "rolled_back"))
    fresh, fresh_inventory, fresh_token, absent = audit(fresh_run)
    result = modify["result"]
    name = result.get("transaction")
    run_ids = proof["run_ids"] + [modify["run_id"], fresh_run["run_id"]]
    if (len(set(run_ids)) != 5 or fresh_token in {before_token, after_token}
            or not isinstance(name, str) or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [0-9a-f]{32}", name)
            or name == proof["transaction"] or fresh_run["result"].get("trial_transaction") != name
            or result.get("status") != "awaiting_bridge_decision" or result["preview_sha256"] != proof["preview_sha256"]
            or result["baseline_audit_token"] != before_token or result["baseline_inventory_sha256"] != inventory
            or (fresh_run["result"].get("kept") is True) != kept):
        raise ValueError("The saved records do not bind an independent native placement decision.")
    digest(result["runtime_sha256"])
    effects, opening_uids = measured(plan, snapshot, result)
    match_replay(proof, result["rows"], effects)
    transactions = modify["changes"].get("transactions")
    if bridge_effects(modify) != effects or not transactions or set(transactions) != {name}:
        raise ValueError("The native decision reports different element effects.")
    if kept:
        if absent or fresh_inventory != digest(result["inventory_sha256"]):
            raise ValueError("The kept project inventory differs from the checked replay.")
        verify_kept(snapshot, fresh, result)
    elif fresh != snapshot or fresh_inventory != inventory or set(absent) != opening_uids:
        raise ValueError("The original project or opening absence was not verified after Discard.")
    receipt = {"status": "mep_openings_kept" if kept else "mep_openings_discarded", "run_ids": run_ids,
               "transaction": name, "preview_sha256": proof["preview_sha256"], "fresh_sha256": fingerprint(fresh),
               "opening_ids": exact_ids(result["effects"]["added"]) if kept else []}
    return dict(receipt, decision_sha256=fingerprint(receipt))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    names = ("choices", "before", "trial", "after", "modify", "fresh")
    for name in names:
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    try:
        records = [read_input(getattr(args, name))[0] for name in names]
        print(json.dumps(verify_decision(*records), indent=2, ensure_ascii=True, allow_nan=False))
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Opening decision verification blocked. {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
