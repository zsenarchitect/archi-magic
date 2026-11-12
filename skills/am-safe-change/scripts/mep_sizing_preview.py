"""Preview nominal MEP dimensions from an explicitly supplied capacity table."""

import argparse
from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys


SHAPES = {
    "duct": {"round": 1, "rectangular": 2, "oval": 2},
    "pipe": {"round": 1},
    "conduit": {"round": 1},
    "cable_tray": {"rectangular": 2},
}
COLUMNS = ["Request", "Service", "Profile", "Demand", "Nominal size", "Rule"]
SCOPE_FIELDS = ("service", "profile", "system", "demand_kind", "demand_unit")
MAX_BYTES = 256 * 1024


def text(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 96 or value != value.strip()
            or any(not character.isprintable() for character in value)):
        raise ValueError("Use nonblank printable labels up to 96 characters.")
    return value


def positive(value):
    if type(value) is int:
        value = Decimal(value)
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("Use positive finite JSON numbers for demand, capacity and dimensions.")
    return value


def scope(row):
    key = tuple(text(row[field]) for field in SCOPE_FIELDS)
    if key[1] not in SHAPES.get(key[0], {}):
        raise ValueError("The service and profile combination is unsupported.")
    return key


def index_rules(rules):
    if not isinstance(rules, list) or not 1 <= len(rules) <= 500:
        raise ValueError("Supply 1 to 500 table rules.")
    groups, ids = defaultdict(list), set()
    for row in rules:
        if set(row) != {*SCOPE_FIELDS, "id", "dimensions", "dimension_unit", "max_demand"}:
            raise ValueError("A table rule has missing or unsupported fields.")
        key = scope(row)
        identifier = text(row["id"])
        dimensions = row["dimensions"]
        if (identifier in ids or not isinstance(dimensions, list)
                or len(dimensions) != SHAPES[key[0]][key[1]]
                or row["dimension_unit"] not in ("mm", "m", "in", "ft")):
            raise ValueError("Table rules need distinct IDs, matching dimensions and explicit length units.")
        ids.add(identifier)
        sizes = " × ".join(str(positive(item)) for item in dimensions)
        groups[key].append((positive(row["max_demand"]), sizes + " " + row["dimension_unit"], identifier))
    return groups


def preview(data):
    if set(data) != {"table_id", "rules", "requests"}:
        raise ValueError("Supply exactly a table ID, table rules and sizing requests.")
    table_id = text(data["table_id"])
    groups = index_rules(data["rules"])
    requests = data["requests"]
    if not isinstance(requests, list) or not 1 <= len(requests) <= 100:
        raise ValueError("Supply 1 to 100 sizing requests.")
    seen, before, after, warnings = set(), [], [], []
    for row in requests:
        if set(row) != {*SCOPE_FIELDS, "id", "demand"}:
            raise ValueError("A sizing request has missing or unsupported fields.")
        identifier = text(row["id"])
        if identifier in seen:
            raise ValueError("Sizing requests need distinct IDs.")
        seen.add(identifier)
        key = scope(row)
        demand = positive(row["demand"])
        candidates = groups.get(key, ())
        match = next((rule for rule in candidates if demand <= rule[0]), None)
        description = [identifier, key[0] + " · " + key[2], key[1], str(demand) + " " + key[4] + " · " + key[3]]
        before.append(description + [None, None])
        if match:
            after.append(description + [match[1], match[2]])
        else:
            after.append(description + [None, None])
            warnings.append({"row": identifier, "blocking": True, "text": (
                "Demand exceeds every matching rule's capacity." if candidates
                else "No rule matches the service, system, profile, demand type and units."
            )})
    return {"kind": "change-preview", "recipe": "mep-sizing", "title": "MEP sizing · " + table_id,
            "keyColumn": "Request", "columns": COLUMNS, "changedColumns": COLUMNS[-2:],
            "before": before, "after": after, "warnings": warnings,
            "review_only": True, "blocked": bool(warnings), "steps": [], "ownership_ids": []}


def unique_object(pairs):
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("The sizing input repeats a JSON field.")
    return obj


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON file with sizing requests and the reviewed capacity table")
    args = parser.parse_args()
    try:
        with args.input.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("The sizing input exceeds 256 KiB.")
        result = preview(json.loads(raw, parse_float=Decimal, object_pairs_hook=unique_object))
        result["basis_sha256"] = sha256(raw).hexdigest()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        print(f"MEP sizing stopped. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 1 if result["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
