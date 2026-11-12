"""Bind a reviewed project batch to exact sealed family exports."""

import argparse
from collections import defaultdict
import sys

from batch_family_export import verified_export
from batch_family_type_preview import build_preview, validate_record
from family_library_discovery import MAX_TOTAL_BYTES, manifest_digest
from family_placement_preview import load_json
from read_family_library import encode_record


def build_load_preview(lookup, project_read, staging, exports):
    classified = build_preview(lookup, project_read)
    validate_record(staging, "family-library-staging", "staging_sha256")
    if staging["discovery_sha256"] != lookup["request"]["discovery_sha256"]:
        raise ValueError("Use the reviewed staging receipt for this batch.")
    if classified["blocked"]:
        raise ValueError("Resolve blocked rows or review a new eligible batch before loading.")
    ready = defaultdict(list)
    skipped = []
    for source, outcome in zip(lookup["rows"], classified["review"]["outcomes"]):
        if outcome["status"] == "skip_existing":
            skipped.append({"line": source["line"], "source_path": source["source_path"],
                            "type": source["type"], "existing": outcome["existing"]})
        elif outcome["status"] == "ready_for_derivation":
            ready[source["source_path"]].append({"line": source["line"], "family": source["family"],
                                                "category": source["category"], "seed_type": source["seed_type"],
                                                "type": source["type"], "sizes": outcome["sizes"]})
    if len(exports) != len(ready):
        raise ValueError("Use one verified export for each source with new types.")
    classified_digest = manifest_digest(classified)
    bound = {}
    staged_bytes = sum(pair["rfa"]["bytes"] + (pair["catalog"]["bytes"] if pair["catalog"] else 0)
                       for pair in staging["pairs"])
    total = staged_bytes
    for workspace in exports:
        with verified_export(workspace, staging) as (_, receipt, derivation):
            source = receipt["source_path"]
            rows = ready.get(source)
            if (source in bound or rows is None or derivation["rows"] != rows
                    or derivation["lookup_sha256"] != lookup["lookup_sha256"]
                    or derivation["preview_sha256"] != classified_digest
                    or derivation["project"] != classified["review"]["project"]
                    or derivation["revit_build"] != classified["review"]["revit_build"]):
                raise ValueError("An export does not match the exact project, approved rows, and source.")
            total += receipt["rfa"]["bytes"] + derivation["working_file"]["bytes"]
            if total > MAX_TOTAL_BYTES:
                raise ValueError("The staged, working, and derived files exceed the batch byte budget.")
            bound[source] = {"receipt": receipt, "rows": rows}
    if set(bound) != set(ready):
        raise ValueError("The batch does not have all reviewed exports.")

    review = {"kind": "batch-family-project-load-review", "version": 1, "lookup_sha256": lookup["lookup_sha256"],
              "classification_sha256": classified_digest, "project": classified["review"]["project"],
              "revit_build": classified["review"]["revit_build"], "staging_sha256": staging["staging_sha256"],
              "exports": [bound[source] for source in ready], "skipped": skipped}
    loads = {row["line"]: group["receipt"]["rfa"]["sha256"] for group in review["exports"] for row in group["rows"]}
    columns = classified["columns"] + ["Derived RFA SHA-256", "Allowed additions"]
    before, after = [], []
    for old, new in zip(classified["before"], classified["after"]):
        digest = loads.get(int(new[0]))
        before.append(old + ["", ""])
        if digest:
            row = list(new)
            row[5] = "Load exact new type"
            after.append(row + [digest, "New family and reviewed types only"])
        else:
            after.append(new + ["", "None"])
    return {"kind": "change-preview", "recipe": "batch-family-project-load", "title": "Review batch family loading",
            "keyColumn": classified["keyColumn"], "columns": columns, "changedColumns": columns[5:],
            "before": before, "after": after, "ownership_ids": [], "blocked": bool(loads),
            "warnings": ([{"row": "", "text": "The exact project load requires a rollback trial.",
                           "blocking": True}] if loads else []),
            "notes": [f"{len(loads)} exact types to trial. {len(skipped)} existing types will be skipped."],
            "review": review, "review_sha256": manifest_digest(review)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("lookup", "project-read", "staging"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--export", action="append", default=[])
    args = parser.parse_args()
    try:
        preview = build_load_preview(load_json(args.lookup), load_json(args.project_read),
                                     load_json(args.staging), args.export)
        print(encode_record(preview).decode("ascii"))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
