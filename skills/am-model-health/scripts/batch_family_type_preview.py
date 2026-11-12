"""Prepare reviewed batch family type lookups and classify the project read."""

import argparse
from collections import defaultdict
import json
import sys

from family_library_discovery import manifest_digest
from family_placement_preview import load_json
from read_family_library import collect_worker, encode_record


def validate_record(record, kind, hash_field):
    basis = {key: value for key, value in record.items() if key != hash_field}
    if (record.get("kind") != kind or record.get("version") != 1
            or record.get(hash_field) != manifest_digest(basis)):
        raise ValueError(f"Use an intact {kind} record.")


def source_read(source, rows, receipt, discovery_sha256):
    metadata = receipt["metadata"]
    names = sorted({size["parameter"] for row in rows for size in row["sizes"]})
    if (metadata["source_path"] != source["path"] or metadata["discovery_sha256"] != discovery_sha256
            or metadata["rfa"] != source["rfa"] or metadata["catalog"] != source["catalog"]
            or metadata["revit_year"] != "2026" or metadata["requested_parameters"] != names):
        raise ValueError("The worker does not match the table's exact source pair and size columns.")
    return metadata


def source_plan(row, metadata):
    result = {"family": None, "category": None, "build": None, "sizes": [], "issues": []}
    if metadata is None:
        result["issues"] = ["The reviewed source has no verified worker receipt."]
        return result
    result.update(family=metadata["family"], category=metadata["category"], build=metadata["revit_build"])
    embedded = {item["name"] for item in metadata["embedded_types"]}
    catalog = {item["name"] for item in metadata["catalog_types"]}
    known_names = {name.casefold() for name in embedded | catalog}
    if (row["seed_type"] not in embedded
            or row["seed_type"] in metadata["catalog_blocked_embedded"]
            or metadata["catalog_coverage"] == "pending"):
        result["issues"].append("The exact embedded seed is unavailable or may be shadowed by a catalog type.")
    if (metadata["embedded_type_coverage"] != "complete"
            or metadata["catalog_coverage"] not in {"absent", "complete"}):
        result["issues"].append("Source type coverage is incomplete.")
    if row["type"].casefold() in known_names:
        result["issues"].append("A type with this name already exists in the source family.")
    for size in row["sizes"]:
        definitions = [field for field in metadata["parameters"] if field["name"] == size["parameter"]]
        if len(definitions) != 1:
            result["issues"].append(f"Size parameter {size['parameter']} is missing or ambiguous.")
            continue
        field = definitions[0]
        result["sizes"].append(dict(size, definition=field))
        if (field["storage"] != "Double" or field["read_only"] or field["formula_driven"] or field["reporting"]):
            result["issues"].append(f"Size parameter {size['parameter']} is outside the editable type scope.")
    return result


def prepare(request, workers):
    validate_record(request, "batch-family-type-request", "request_sha256")
    rows_by_source = defaultdict(list)
    for row in request["rows"]:
        rows_by_source[row["source_path"]].append(row)
    sources = {source["path"]: source for source in request["sources"]}
    if set(rows_by_source) != set(sources) or len(workers) > len(sources):
        raise ValueError("The table rows and reviewed sources disagree.")
    metadata = {}
    for path in workers:
        receipt = collect_worker(path)
        key = receipt["metadata"]["source_path"]
        if key not in sources or key in metadata:
            raise ValueError("Use at most one worker receipt for each exact table source.")
        metadata[key] = source_read(sources[key], rows_by_source[key], receipt, request["discovery_sha256"])
    by_family = defaultdict(set)
    for path, data in metadata.items():
        by_family[data["family"].casefold()].add(path)
    rows = []
    for row in request["rows"]:
        data = metadata.get(row["source_path"])
        source = source_plan(row, data)
        if data and len(by_family[data["family"].casefold()]) > 1:
            source["issues"].append("Distinct source files resolve to the same project family name.")
        rows.append({"line": row["line"], "source_path": row["source_path"], "seed_type": row["seed_type"],
                     "type": row["type"], **source})
    prepared = {"kind": "batch-family-type-lookup", "version": 1, "request": request, "rows": rows}
    return dict(prepared, lookup_sha256=manifest_digest(prepared))


def build_preview(lookup, raw_read):
    validate_record(lookup, "batch-family-type-lookup", "lookup_sha256")
    response = raw_read.get("result", raw_read)
    read = json.loads(response["snapshot_json"]) if "snapshot_json" in response else response
    if (read.get("kind") != "batch-family-type-project-read" or read.get("version") != 1
            or read["lookup_sha256"] != lookup["lookup_sha256"] or read["revit_year"] != "2026"):
        raise ValueError("Use the project read for this exact batch lookup.")
    if [row["line"] for row in read["rows"]] != [row["line"] for row in lookup["rows"]]:
        raise ValueError("The project read did not return every batch row in order.")
    before, after, warnings, outcomes = [], [], [], []
    for row, found, requested in zip(lookup["rows"], read["rows"], lookup["request"]["rows"]):
        issues = list(found["issues"]) + list(row["issues"])
        existing = found["existing"]
        differences = found["differences"]
        values = ", ".join(f"{size['parameter']}={size['value']} [{size['unit']}]" for size in requested["sizes"])
        status = "skip_existing" if existing else ("blocked" if issues else "ready_for_derivation")
        key = str(row["line"])
        category = row["category"] or {}
        identity = [key, row["source_path"], row["family"] or "", category.get("name", category.get("id", "")),
                    row["type"]]
        before.append(identity + ["Existing" if existing else "", "", ""])
        after.append(identity + [status, values, "; ".join(differences)])
        warnings.extend({"row": key, "text": issue, "blocking": status == "blocked"} for issue in issues)
        outcomes.append({"line": row["line"], "status": status, "existing": existing, "sizes": found["sizes"]})
    columns = ["CSV line", "Source", "Family", "Category", "Type", "Action", "Requested values", "Existing differences"]
    payload = {"kind": "batch-family-type-review", "version": 1, "lookup_sha256": lookup["lookup_sha256"],
               "project": read["project"], "revit_build": read["revit_build"], "outcomes": outcomes}
    ready = sum(row["status"] == "ready_for_derivation" for row in outcomes)
    return {
        "kind": "change-preview", "recipe": "batch-family-types", "title": "Review batch family types",
        "keyColumn": "CSV line", "columns": columns, "changedColumns": columns[5:],
        "before": before, "after": after, "warnings": warnings,
        "notes": [f"{ready} rows are ready for a derivation preview. Existing types stay unchanged."],
        "blocked": any(row["status"] == "blocked" for row in outcomes),
        "ownership_ids": [], "review": payload, "review_sha256": manifest_digest(payload),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inputs = commands.add_parser("prepare")
    inputs.add_argument("--request", required=True)
    inputs.add_argument("--worker", action="append", default=[])
    preview = commands.add_parser("preview")
    preview.add_argument("--lookup", required=True)
    preview.add_argument("--project-read", required=True)
    args = parser.parse_args()
    try:
        result = (prepare(load_json(args.request), args.worker) if args.command == "prepare"
                  else build_preview(load_json(args.lookup), load_json(args.project_read)))
        print(encode_record(result).decode("ascii"))
    except (AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
