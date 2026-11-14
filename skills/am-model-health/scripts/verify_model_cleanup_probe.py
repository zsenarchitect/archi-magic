"""Verify a Revit cleanup probe against its reviewed selection and independent reread."""

import argparse
import json
import re
import sys

from cad_layout_preview import read_input
from cleanup_preview import MAX_BYTES, build_preview, positive_id
from revit_probe_record import require_no_changes, require_run


def effect_ids(changes, field, expected):
    samples = changes.get(field)
    count = changes.get(field + "_count")
    if (not isinstance(samples, list) or type(count) is not int or len(samples) != count
            or len(samples) > len(expected)):
        raise ValueError("The runtime change samples are incomplete or exceed the reviewed selection.")
    ids = []
    for sample in samples:
        if field == "deleted":
            value = sample
        elif isinstance(sample, dict):
            value = sample.get("id")
        else:
            raise ValueError("The runtime recorded an invalid element sample.")
        if type(value) is int:
            value = str(value)
        if not positive_id(value) or value not in expected:
            raise ValueError("The runtime recorded an element outside the reviewed selection.")
        ids.append(value)
    if len(set(ids)) != count:
        raise ValueError("The runtime change samples contain repeated element IDs.")
    return set(ids)


def require_probe_effects(record, expected, transaction):
    if record["decision"] == "no_changes":
        require_no_changes(record, "dry_run")
        return
    changes = record.get("changes")
    if (not isinstance(changes, dict) or any(changes.get(field) for field in
            ("other_documents", "added_ids", "modified_ids", "deleted_ids"))
            or changes.get("transactions") != [transaction]):
        raise ValueError("The runtime changes are not attributed solely to the cleanup probe.")
    selected = set(expected)
    if effect_ids(changes, "deleted", selected) != selected:
        raise ValueError("The runtime deletion set differs from the reviewed selection.")
    # The host accumulates events from both the inner commit and group rollback.
    added = effect_ids(changes, "added", selected)
    modified = effect_ids(changes, "modified", selected)
    categories = changes.get("by_category")
    if (not isinstance(categories, dict) or any(type(count) is not int or count <= 0 for count in categories.values())
            or sum(categories.values()) != len(added | modified)):
        raise ValueError("The runtime category counts do not match the recorded effects.")


def verify(preview, before_run, dry_run, fresh_run):
    before = require_no_changes(before_run, "query")
    dry = require_run(dry_run, "dry_run", ("no_changes", "rolled_back"))
    fresh = require_no_changes(fresh_run, "query")
    if len({record["run_id"] for record in (before, dry, fresh)}) != 3:
        raise ValueError("Use three separate Revit runs for the original inventory, probe, and reread.")
    selected = preview["cleanup_selection"]["selected_ids"]
    reviewed = build_preview(before["result"], selected)
    if preview != reviewed:
        raise ValueError("The reviewed preview differs from the original inventory and selection.")
    selection = reviewed["cleanup_selection"]
    expected = selection["selected_ids"]
    if selection["probe_candidate_ids"] != expected:
        raise ValueError("The reviewed selection includes blocked items.")
    after = build_preview(fresh["result"], selected)
    if after != reviewed:
        raise ValueError("The independent inventory differs after rollback.")

    result = dry["result"]
    transaction = result.get("transaction")
    if not isinstance(transaction, str) or re.fullmatch(r"Probe Revit cleanup [0-9a-f]{32}", transaction) is None:
        raise ValueError("The rollback probe transaction name is missing or invalid.")
    expected_receipt = {"status": "probe_rolled_back", "rollback_verified": True,
                        "basis_sha256": reviewed["basis_sha256"], "document": selection["document"],
                        "temporary_deleted_ids": expected, "transaction": transaction}
    if result.get("rollback_verified") is not True or result != expected_receipt:
        raise ValueError("The rollback probe does not prove the exact reviewed deletion and restoration.")
    require_probe_effects(dry, expected, transaction)
    return {"status": "rollback_probe_verified", "basis_sha256": reviewed["basis_sha256"],
            "before_run_id": before["run_id"], "dry_run_id": dry["run_id"], "fresh_run_id": fresh["run_id"],
            "temporary_deleted_ids": expected, "document": selection["document"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preview", help="Reviewed cleanup preview JSON")
    parser.add_argument("before", help="Full original Revit inventory run")
    parser.add_argument("probe", help="Full Revit cleanup dry-run record")
    parser.add_argument("fresh", help="Full independent inventory run after rollback")
    args = parser.parse_args()
    try:
        preview, before, probe, fresh = [read_input(path, 2 * MAX_BYTES)[0]
                                        for path in (args.preview, args.before, args.probe, args.fresh)]
        print(json.dumps(verify(preview, before, probe, fresh), ensure_ascii=True, indent=2))
    except (AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError) as error:
        print("Cleanup rollback verification stopped: {}".format(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
