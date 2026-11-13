"""Show every planned deletion and check the exact IDs from a Revit dry run."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


def element_id(value):
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value):
        raise ValueError("Use a positive Revit ElementId for each deletion.")
    if int(value) > 2 ** 63 - 1:
        raise ValueError("A deletion ID exceeds the Revit ElementId range.")
    return value


def planned_deletions(preview):
    if preview.get("kind") != "change-preview":
        raise ValueError("Use the exact saved change preview.")
    rows = preview.get("deletions", [])
    if not isinstance(rows, list):
        raise ValueError("List each deleted element separately in deletions.")
    by_id = {}
    for row in rows:
        identifier = element_id(row["id"])
        if identifier in by_id:
            raise ValueError(f"Deletion {identifier} is listed more than once.")
        if any(not isinstance(row[key], str) or not row[key].strip() for key in ("category", "name")):
            raise ValueError(f"Name the category and exact element for deletion {identifier}.")
        by_id[identifier] = row
    return by_id


def delete_list(by_id):
    def display(value):
        return value if value.isprintable() else json.dumps(value, ensure_ascii=False)

    count = len(by_id)
    return "\n".join([f"Delete: {count} {'element' if count == 1 else 'elements'}", *(
        f"- ID {identifier} | {display(row['category'])} | {display(row['name'])}"
        for identifier, row in by_id.items()
    )])


def verify_dry_run(by_id, saved):
    run = saved.get("response", saved)
    run_id = run.get("run_id")
    exit_code = run.get("exit_code")
    if (run.get("mode") != "dry_run" or run.get("status") != "ok" or run.get("decision") != "rolled_back"
            or not isinstance(run_id, str) or not run_id.strip() or type(exit_code) is not int or exit_code != 0
            or run.get("approval") is not None or run.get("result_truncated") is not False
            or "error" not in run or run["error"] is not None
            or any(run.get(field) != [] for field in (
                "blocked", "failures", "dialogs", "warnings", "closed_documents", "unclosed_documents"))):
        raise ValueError("Save the complete successful rolled-back Revit dry run.")
    changes = run["changes"]
    count = changes["deleted_count"]
    rows = changes["deleted"]
    if (not isinstance(rows, list) or type(count) is not int or count != len(rows)
            or changes.get("deleted_ids") or changes.get("other_documents")):
        raise ValueError("The dry run did not return one complete list of deleted elements in the reviewed model.")
    # pyRevit emits bare IDs for deleted elements. Saved evidence can also have named rows.
    actual = {element_id(row["id"] if isinstance(row, dict) else row) for row in rows}
    if len(actual) != count:
        raise ValueError("The dry run repeated a deletion ID.")
    if actual != by_id.keys():
        extra = ", ".join(sorted(actual - by_id.keys(), key=int)) or "none"
        missing = ", ".join(sorted(by_id.keys() - actual, key=int)) or "none"
        raise ValueError(f"The dry run differs from the reviewed delete list. Extra IDs: {extra}. Missing IDs: {missing}.")


def load(path):
    with open(path, encoding="utf-8-sig") as stream:
        return json.load(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preview")
    parser.add_argument("--dry-run")
    parser.add_argument("--approved-preview-sha256", help="SHA-256 printed when the user reviewed the preview")
    args = parser.parse_args()
    try:
        preview = Path(args.preview).read_bytes()
        digest = hashlib.sha256(preview).hexdigest()
        if args.dry_run and not args.approved_preview_sha256:
            raise ValueError("Provide the preview SHA-256 recorded when the user approved the deletion list.")
        if args.approved_preview_sha256 is not None and args.approved_preview_sha256 != digest:
            raise ValueError("The preview differs from the approved file. Show the new preview and ask again.")
        rows = planned_deletions(json.loads(preview.decode("utf-8-sig")))
        if args.dry_run:
            verify_dry_run(rows, load(args.dry_run))
        print(delete_list(rows))
        print(f"Preview SHA-256: {digest}")
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as error:
        print(f"Delete approval blocked. {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
