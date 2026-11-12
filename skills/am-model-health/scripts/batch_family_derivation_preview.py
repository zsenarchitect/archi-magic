"""Build a review-only family derivation preview from the active working-copy read."""

import argparse
import json
import sys

from batch_family_type_preview import validate_record
from family_library_discovery import manifest_digest
from family_placement_preview import load_json
from read_family_library import encode_record


def working_read(value):
    response = value.get("response", value)
    response = response.get("result", response)
    return json.loads(response["snapshot_json"]) if "snapshot_json" in response else response


def build_preview(plan, raw_read):
    validate_record(plan, "batch-family-type-derivation", "derivation_sha256")
    read = working_read(raw_read)
    validate_record(read, "batch-family-working-read", "working_read_sha256")
    if (read["derivation_sha256"] != plan["derivation_sha256"] or read["revit_build"] != plan["revit_build"]
            or read["inventory"] is None):
        raise ValueError("Use a complete family query for this exact working copy and Revit build.")
    rows, notes = [], []
    state = read["state"]
    calculated = sorted(field["name"] for field in state["parameters"].values()
                        if field["formula_driven"] or field["reporting"])
    if calculated:
        notes.append("Revit must measure the new values for these calculated parameters: " + ", ".join(calculated))
    before, after = [], []
    for row in plan["rows"]:
        seed_values = state["types"][row["seed_type"]]
        changes = []
        for size in row["sizes"]:
            key = size["definition"]["id"]
            old = seed_values[key]
            changes.append({"id": key, "parameter": size["parameter"], "before": old,
                            "input": size["value"], "unit": size["unit"], "internal_value": size["internal_value"]})
        line = str(row["line"])
        label = [line, row["family"], row["seed_type"], row["type"]]
        before.append(label + ["Type absent", ""])
        after.append(label + ["Create in working copy", ", ".join(
            f"{size['parameter']}={size['input']} [{size['unit']}]" for size in changes
        )])
        rows.append({"line": row["line"], "seed_type": row["seed_type"], "type": row["type"], "sizes": changes})
    columns = ["CSV line", "Family", "Seed", "New type", "Action", "Approved size values"]
    review = {
        "kind": "batch-family-derivation-review", "version": 1, "derivation_sha256": plan["derivation_sha256"],
        "working_read_sha256": read["working_read_sha256"], "owner": read["owner"], "rows": rows,
    }
    return {"kind": "change-preview", "recipe": "batch-family-derivation", "title": "Review family type derivation",
            "keyColumn": "CSV line", "columns": columns, "changedColumns": columns[4:],
            "before": before, "after": after, "notes": notes,
            "warnings": [{"row": "", "text": "Native seed inheritance and exact effects require a rollback trial.",
                          "blocking": True}],
            "blocked": True, "ownership_ids": [read["owner"]["id"]],
            "review": review, "review_sha256": manifest_digest(review)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--derivation", required=True)
    parser.add_argument("--family-read", required=True)
    args = parser.parse_args()
    try:
        print(encode_record(build_preview(load_json(args.derivation), load_json(args.family_read))).decode("ascii"))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
