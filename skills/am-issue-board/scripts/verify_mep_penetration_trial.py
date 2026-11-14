"""Verify an exact Revit opening trial and an independent restored project read."""

import argparse
import json
import math
from pathlib import Path
import re
import sys

from mep_penetration_preview import fingerprint
from mep_penetration_spacing import scalar
from probe_mep_penetration_placement import TRANSACTION_NAME, placement_plan
from read_mep_penetrations import area, matches_outline
from structural_compare_cli import checked_id, digest, read_input, uid


def require_run(raw, mode, decisions, *, approval=None, status="ok"):
    run = raw.get("response", raw)
    if (run.get("mode") != mode or run.get("status") != status or run.get("decision") not in decisions
            or type(run.get("exit_code")) is not int or run["exit_code"] != 0
            or run.get("result_truncated") is not False or run.get("error") is not None
            or run.get("approval") != approval or run.get("engine", {}).get("requested") != "cpython"
            or any(run.get(key) for key in ("blocked", "warnings", "failures", "dialogs", "unclosed_documents"))):
        raise ValueError("Use a complete CPython Revit run with the expected mode and no warnings.")
    identifier = run.get("run_id")
    if not isinstance(identifier, str) or not identifier.isprintable() or not identifier.strip() or len(identifier) > 160:
        raise ValueError("Use a complete saved Revit run ID.")
    if not isinstance(run.get("result"), dict):
        raise ValueError("The saved Revit result is unavailable.")
    return run


def exact_ids(rows):
    if not isinstance(rows, list):
        raise ValueError("The saved change report does not contain its full element list.")
    ids = [checked_id(value) for value in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("The change report repeats an element ID.")
    return sorted(ids, key=int)


def bridge_effects(run):
    changes = run["changes"]
    result = {}
    if changes.get("other_documents"):
        raise ValueError("The Revit run reports changes in another document.")
    for kind in ("added", "deleted", "modified"):
        rows = changes.get(kind, [])
        count = changes[kind + "_count"]
        if (type(count) is not int or not isinstance(rows, list) or count != len(rows)
                or changes.get(kind + "_ids")):
            raise ValueError("Save the complete Revit effect list with every element ID.")
        result[kind] = exact_ids([str(row["id"]) if type(row["id"]) is int else row["id"] for row in rows])
    return result


def audit(run):
    if any(bridge_effects(run).values()) or run["changes"].get("transactions"):
        raise ValueError("The independent project audit reports model changes.")
    result = run["result"]
    token = result.get("audit_token")
    if result.get("status") != "ok" or not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Save a fresh query with the complete project audit token.")
    inventory = digest(result["inventory_sha256"])
    absent = result["absent_opening_uids"]
    if not isinstance(absent, list) or len(absent) > 10 or len({uid(value) for value in absent}) != len(absent):
        raise ValueError("The audit does not report exact absent opening UIDs.")
    return json.loads(result["snapshot_json"]), inventory, token, absent


def measured(plan, snapshot, trial):
    rows = trial["rows"]
    reviewed = plan["rows"]
    if not isinstance(rows, list) or len(rows) != len(reviewed):
        raise ValueError("The trial did not measure each reviewed opening.")
    tolerance = snapshot["tolerance_ft"]
    opened, unique, activated = set(), set(), set()
    for row, expected in zip(rows, reviewed):
        identifier, unique_id = checked_id(row["opening_id"]), uid(row["opening_uid"])
        if identifier in opened or unique_id in unique or row["crossing_key"] != expected["crossing_key"]:
            raise ValueError("The trial reports a repeated or unexpected opening.")
        opened.add(identifier)
        unique.add(unique_id)
        digest(row["opening_sha256"])
        digest(row["tracking_sha256"])
        geometry = expected["geometry"]
        cut = row["cut_outline"]
        volume = scalar(row["removed_volume_ft3"])
        if (not matches_outline(cut, geometry["outline"], tolerance) or volume <= 0
                or abs(volume - area(cut) * geometry["depth_ft"]) > tolerance * area(cut)):
            raise ValueError("The measured cut or removed material differs from the reviewed opening.")
        symbol = expected["symbol"]
        if row["activated_symbol"] is not None:
            if row["activated_symbol"] != symbol["id"] or symbol["is_active"] or symbol["id"] in activated:
                raise ValueError("An unreviewed symbol activation was reported.")
            activated.add(symbol["id"])
    expected_activations = {row["symbol"]["id"] for row in reviewed if row["symbol"]["is_active"] is False}
    if activated != expected_activations:
        raise ValueError("The symbol activation receipt does not match the baseline.")
    effects = trial["effects"]
    if effects.keys() != {"added", "modified", "deleted"}:
        raise ValueError("The trial effect receipt is incomplete.")
    effects = {key: exact_ids(values) for key, values in effects.items()}
    hosts = {row["host_id"] for row in reviewed}
    modified = set(effects["modified"])
    if set(effects["added"]) != opened or effects["deleted"] or not hosts <= modified or modified - hosts - activated - opened:
        raise ValueError("The measured effects exceed the reviewed opening and host scope.")
    return effects, unique


def verify(choices, before_raw, trial_raw, after_raw):
    before = require_run(before_raw, "query", ("no_changes", "rolled_back"))
    dry = require_run(trial_raw, "dry_run", ("no_changes", "rolled_back"))
    after = require_run(after_raw, "query", ("no_changes", "rolled_back"))
    run_ids = [run["run_id"] for run in (before, dry, after)]
    if len(set(run_ids)) != 3:
        raise ValueError("Use separate Revit runs for the baseline, trial, and restored project.")
    snapshot, inventory, before_token, original_absent = audit(before)
    fresh, fresh_inventory, after_token, absent = audit(after)
    plan = placement_plan(snapshot, choices)
    result = dry["result"]
    name = result.get("transaction")
    if (not isinstance(name, str) or not re.fullmatch(re.escape(TRANSACTION_NAME) + r" [0-9a-f]{32}", name)
            or result.get("status") != "trial_rolled_back" or result.get("rollback_verified") is not True
            or result.get("preview_sha256") != fingerprint(plan) or result.get("baseline_audit_token") != before_token
            or result.get("baseline_inventory_sha256") != inventory or before_token == after_token
            or fresh != snapshot or fresh_inventory != inventory or original_absent
            or before["result"].get("trial_transaction") is not None or after["result"].get("trial_transaction") != name
            or before["result"].get("kept") is True or after["result"].get("kept") is True):
        raise ValueError("The independent audit does not prove rollback of the exact reviewed placement.")
    effects, opening_uids = measured(plan, snapshot, result)
    if set(absent) != opening_uids:
        raise ValueError("The fresh query did not verify absence of every trial opening.")
    bridge = bridge_effects(dry)
    transactions = dry["changes"].get("transactions", [])
    if any(bridge.values()):
        if dry["decision"] != "rolled_back" or bridge != effects or transactions != [name]:
            raise ValueError("The bridge and trial report different provisional effects.")
    elif transactions and (dry["decision"] != "rolled_back" or transactions != [name]):
        raise ValueError("The bridge reports a different provisional transaction.")
    proof = {"status": "mep_opening_trial_verified", "run_ids": run_ids, "transaction": name,
             "preview_sha256": fingerprint(plan), "inventory_sha256": inventory,
             "effects": effects, "measurements": result["rows"]}
    return dict(proof, proof_sha256=fingerprint(proof))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("choices", "before", "trial", "after"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    try:
        records = [read_input(getattr(args, name))[0] for name in ("choices", "before", "trial", "after")]
        print(json.dumps(verify(*records), ensure_ascii=True, allow_nan=False, indent=2))
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Opening trial verification blocked. {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
