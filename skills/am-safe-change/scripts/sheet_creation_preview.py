"""Plan new Revit sheets from a complete, read-only sheet creation snapshot."""

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import unicodedata
from uuid import UUID

from view_duplicate_preview import element_id, read_payload

MAX_BYTES = 2 * 1024 * 1024
MAX_SHEETS = 10000
MAX_TYPES = 1000
MAX_ROWS = 50
MAX_PARAMETERS = 20
PROHIBITED_NUMBER = frozenset("{}[]|;<>?`~")
SHEET_SCOPE = ("sheet", "title_block")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def text(value, limit=200, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not value and not empty)
            or not all(char.isprintable() for char in value)):
        raise ValueError("Use bounded display text without control characters.")
    return value


def name(value):
    value = text(value)
    if value != value.strip():
        raise ValueError("Use a number or name without surrounding whitespace.")
    return value


def number_key(number):
    return unicodedata.normalize("NFC", number).casefold()


def token(value, pattern):
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError("An identifier is missing or has an invalid format.")
    return value


def uid(value):
    return token(value, r"[0-9a-fA-F-]{1,100}")


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
        raise ValueError("A sheet creation record has missing or unexpected fields.")


def bounded(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"Expected a list of up to {limit} records.")
    return value


def encode(value):
    data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(data) > MAX_BYTES:
        raise ValueError("The sheet creation data exceeds 2 MiB.")
    return data


def normalize_assignment(assignment):
    fields(assignment, ("scope", "parameter", "kind", "value"), ("unit",))
    scope = assignment["scope"]
    if scope not in SHEET_SCOPE:
        raise ValueError("Choose sheet or title_block instance scope.")
    parameter = assignment["parameter"]
    fields(parameter, ("kind", "id"), ("uid",))
    identity = parameter["id"]
    if parameter["kind"] == "guid" and "uid" not in parameter:
        identity = str(UUID(token(identity, r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")))
    elif parameter["kind"] == "built_in" and "uid" not in parameter:
        identity = token(identity, r"[A-Z][A-Z0-9_]{0,99}")
        if identity in ("SHEET_NUMBER", "SHEET_NAME", "SHEET_PRIMARY_TITLE_BLOCK"):
            raise ValueError("Use the dedicated sheet fields for number, name, and title block.")
    elif parameter["kind"] == "project" and "uid" in parameter:
        element_id(identity)
        uid(parameter["uid"])
    else:
        raise ValueError("Use a shared GUID, built-in identity, or project parameter ID and UID.")

    kind, value = assignment["kind"], assignment["value"]
    if (kind == "double") != ("unit" in assignment):
        raise ValueError("A measured double requires an explicit unit.")
    if kind == "string":
        text(value, 2000, empty=True)
    elif kind == "integer" and type(value) is int and -(2**31) <= value < 2**31:
        pass
    elif kind == "yes_no" and type(value) is bool:
        pass
    elif kind == "double" and type(value) in (int, float) and -math.inf < value < math.inf:
        text(assignment["unit"], 150)
    elif kind == "element_id":
        fields(value, ("id", "uid", "category_id"))
        element_id(value["id"])
        uid(value["uid"])
        token(value["category_id"], r"-[1-9][0-9]{0,18}")
    else:
        raise ValueError("The assignment value does not match its declared storage kind.")
    return {**assignment, "parameter": {**parameter, "id": identity}}


def normalize_request(request):
    fields(request, ("rows",), ("source",))
    rows = bounded(request["rows"], MAX_ROWS)
    if not rows:
        raise ValueError("Request at least one sheet.")
    source = request.get("source")
    if "source" in request:
        fields(source, ("sha256", "file", "sheet", "header_row", "mapping"), ("mapping_sha256",))
        token(source["sha256"], SHA256)
        if "mapping_sha256" in source:
            token(source["mapping_sha256"], SHA256)
        text(source["file"])
        text(source["sheet"])
        if type(source["header_row"]) is not int or not 1 <= source["header_row"] <= 5000:
            raise ValueError("The workbook header row is invalid.")
        if not isinstance(source["mapping"], dict) or not source["mapping"]:
            raise ValueError("Record the reviewed workbook column mapping.")
        for key, column in source["mapping"].items():
            name(key)
            name(column)
    normalized, positions = [], set()
    for index, row in enumerate(rows, 1):
        fields(row, ("number", "name", "title_block_type_id"), ("parameters", "source_row"))
        number = name(row["number"])
        if PROHIBITED_NUMBER.intersection(number):
            raise ValueError("A sheet number contains a character prohibited by Revit.")
        position = row.get("source_row", index)
        if (type(position) is not int or not 1 <= position <= 5000 or position in positions
                or (source is not None and ("source_row" not in row or position <= source["header_row"]))):
            raise ValueError("Every row needs a distinct valid source row.")
        positions.add(position)
        assignments = [normalize_assignment(item) for item in bounded(row.get("parameters", []), MAX_PARAMETERS)]
        selectors = {(item["scope"], item["parameter"]["kind"], item["parameter"]["id"]) for item in assignments}
        if len(selectors) != len(assignments):
            raise ValueError("An instance parameter is assigned more than once in a row.")
        normalized.append({"number": number, "name": name(row["name"]), "source_row": position,
                           "title_block_type_id": element_id(row["title_block_type_id"]), "parameters": assignments})
    return {**request, "rows": normalized}


def inventory(snapshot):
    types = {}
    sheets = defaultdict(list)
    known_ids = set()
    for item in bounded(snapshot["title_blocks"], MAX_TYPES):
        fields(item, ("id", "uid", "family", "name"))
        key = element_id(item["id"])
        uid(item["uid"])
        text(item["family"])
        text(item["name"])
        if key in known_ids:
            raise ValueError("The inventory repeats an element ID.")
        known_ids.add(key)
        types[key] = item
    for sheet in bounded(snapshot["sheets"], MAX_SHEETS):
        fields(sheet, ("id", "uid", "number", "name", "placeholder"), ("title_block_type_id", "parameters"))
        key = element_id(sheet["id"])
        uid(sheet["uid"])
        number = text(sheet["number"])
        text(sheet["name"])
        if key in known_ids or type(sheet["placeholder"]) is not bool:
            raise ValueError("The sheet inventory has a duplicate or invalid row.")
        known_ids.add(key)
        if sheet.get("title_block_type_id") is not None:
            element_id(sheet["title_block_type_id"])
        if "parameters" in sheet and sheet["parameters"] is not None:
            for item in bounded(sheet["parameters"], MAX_PARAMETERS):
                normalize_assignment(item)
        sheets[number_key(number)].append(sheet)
    return types, sheets


def title_label(item):
    return f"{item['family']} / {item['name']} (ID {item['id']})" if item else ""


def display_parameters(assignments):
    if assignments is None:
        return "Values unavailable"
    return " | ".join(
        f"{item['scope']} {item['parameter']['id']} = {json.dumps(item['value'], ensure_ascii=False, sort_keys=True)}"
        + (f" {item['unit']}" if "unit" in item else "") for item in assignments
    )


def compute(snapshot):
    fields(snapshot, ("kind", "complete", "document", "request", "title_blocks", "sheets"))
    if snapshot["kind"] != "sheet-creation-snapshot" or snapshot["complete"] is not True:
        raise ValueError("Supply a complete sheet creation snapshot.")
    document = snapshot["document"]
    fields(document, ("project_uid", "path_sha256"))
    uid(document["project_uid"])
    token(document["path_sha256"], SHA256)
    request = normalize_request(snapshot["request"])
    encoded = encode({**snapshot, "request": request})
    types, sheets = inventory(snapshot)
    counts = Counter(number_key(row["number"]) for row in request["rows"])
    before, after, warnings, steps = [], [], [], []
    for row in request["rows"]:
        number, position = row["number"], str(row["source_row"])
        key = number_key(number)
        title_block = types.get(row["title_block_type_id"])
        existing = sheets.get(key, [])
        original = existing[0] if len(existing) == 1 else None
        old_label = title_label(types.get(original.get("title_block_type_id"))) if original else ""
        old_ids = ", ".join(item["id"] for item in existing)
        old_number = ", ".join(item["number"] for item in existing)
        current = [position, old_number, original["name"] if original else "", old_label,
                   display_parameters(original.get("parameters")) if original else "", old_ids, ""]
        if counts[key] > 1 or title_block is None:
            reason = ("The requested number appears more than once." if counts[key] > 1
                      else "The requested title block type is absent from the inventory.")
            warnings.append({"row": position, "text": reason, "blocking": True})
            action = "Blocked"
        elif existing:
            matches = (original is not None and not original["placeholder"]
                       and original["number"] == number and original["name"] == row["name"]
                       and original.get("title_block_type_id") == row["title_block_type_id"]
                       and (not row["parameters"] or original.get("parameters") == row["parameters"]))
            action = "Already matches" if matches else "Skip number conflict"
            if not matches:
                warnings.append({"row": position, "text": f"Requested {number} conflicts with sheet number {old_number} at ID {old_ids}.",
                                 "blocking": False})
        else:
            action = "Create"
            steps.append({**row, "title_block_type_uid": title_block["uid"]})
        before.append(current)
        proposed = [position, number, row["name"], title_label(title_block), display_parameters(row["parameters"]), old_ids]
        after.append((current[:-1] if existing else proposed) + [action])

    blocked = any(item["blocking"] for item in warnings)
    columns = ["Source row", "Sheet number", "Sheet name", "Title block", "Instance parameters", "Existing IDs", "Action"]
    preview = {"kind": "change-preview", "recipe": "sheet-creation", "title": "Create sheets from a list",
               "columns": columns, "keyColumn": columns[0], "changedColumns": columns[1:],
               "before": before, "after": after, "warnings": warnings, "blocked": blocked,
               "ownership_ids": [], "steps": [] if blocked else steps, "document": document, "request": request,
               "basis_sha256": hashlib.sha256(encoded.encode("ascii")).hexdigest()}
    encode(preview)
    return preview


def check_receipt(plan, receipt):
    fields(receipt, ("document", "basis_sha256", "created"))
    if (plan.get("recipe") != "sheet-creation" or plan["blocked"] is not False
            or not plan["steps"] or receipt["document"] != plan["document"]
            or receipt["basis_sha256"] != plan["basis_sha256"]):
        raise ValueError("The sheet receipt does not belong to the approved batch.")
    created = bounded(receipt["created"], MAX_ROWS)
    if len(created) != len(plan["steps"]):
        raise ValueError("The receipt does not cover every reviewed sheet and title block.")
    identifiers, identities = set(), set()
    for row in created:
        fields(row, ("sheet_id", "sheet_uid", "title_block_id", "title_block_uid"))
        identifiers.update((element_id(row["sheet_id"]), element_id(row["title_block_id"])))
        identities.update((uid(row["sheet_uid"]), uid(row["title_block_uid"])))
    if len(identifiers) != 2 * len(created) or len(identities) != len(identifiers):
        raise ValueError("The sheet receipt repeats element identities.")
    return identifiers


def load(path, *, envelope=False):
    limit = 2 * MAX_BYTES + 65536 if envelope else MAX_BYTES
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("The sheet creation input exceeds its size limit.")
    data = json.loads(raw.decode("utf-8-sig"))
    if isinstance(data, dict):
        return data.get("response", data)
    return data


def snapshot_payload(data):
    if isinstance(data, dict) and "result" in data:
        return read_payload(data, "snapshot_json")
    return data


def checked_run(record, outcome, *, mode=None):
    expected = "rejected" if outcome == "discarded" else "ok"
    exit_code = record.get("exit_code")
    if (record.get("decision") != outcome or record.get("status") != expected
            or (mode is not None and (record.get("mode") != mode or record.get("result_truncated") is not False
                                     or type(exit_code) is not int or exit_code != 0
                                     or (mode != "modify" and record.get("approval") is not None)))
            or (outcome == "discarded" and record.get("approval") != "user")
            or (outcome == "committed" and record.get("approval") not in ("user", "auto"))
            or any(record.get(field) for field in ("error", "failures", "dialogs", "blocked", "warnings",
                                                    "unclosed_documents"))):
        raise ValueError("The full pyRevit record does not establish the required outcome.")
    changes = record["changes"]
    if changes.get("other_documents") or any(type(changes[key]) is not int for key in
                                              ("added_count", "modified_count", "deleted_count")):
        raise ValueError("The runtime change scope could not be verified.")
    return changes


def runtime_receipt(plan, record, outcome):
    changes = checked_run(record, outcome, mode="dry_run" if outcome == "rolled_back" else "modify")
    receipt = read_payload(record, "receipt_json")
    ids = check_receipt(plan, receipt)
    added = [row["id"] for row in changes.get("added", [])]
    legacy_added = changes.get("added_ids")
    if legacy_added is not None:
        if "added" in changes and sorted(map(str, added)) != sorted(map(str, legacy_added)):
            raise ValueError("The runtime change record has conflicting addition lists.")
        added = legacy_added
    if (not isinstance(added, list) or any(type(item) not in (int, str) for item in added)
            or changes["added_count"] != len(added) or len(added) != len(ids)
            or {element_id(str(item)) for item in added} != ids
            or changes["modified_count"] or changes["deleted_count"]
            or any(changes.get(field) for field in ("modified", "deleted", "modified_ids", "deleted_ids"))
            or any(type(value) is not int or value <= 0 for value in changes["by_category"].values())
            or sum(changes["by_category"].values()) != len(ids)):
        raise ValueError("The runtime changes differ from the exact reviewed sheet and title block additions.")
    return receipt


def verify_outcome(plan, record, fresh, outcome, dry=None):
    runs = (record, fresh) if dry is None else (record, fresh, dry)
    run_ids = [text(run.get("run_id")) for run in runs]
    if len(set(run_ids)) != len(run_ids):
        raise ValueError("Use distinct Revit runs for the dry run, real run, and independent read.")
    receipt = runtime_receipt(plan, record, outcome)
    changes = checked_run(fresh, "rolled_back", mode="query")
    if (any(changes[key] for key in ("added_count", "modified_count", "deleted_count"))
            or any(changes.get(key) for key in ("added", "modified", "deleted", "added_ids", "modified_ids",
                                               "deleted_ids", "by_category"))):
        raise ValueError("The independent Revit query reported model changes.")
    if outcome == "committed":
        if dry is None:
            raise ValueError("Supply the full dry-run record for comparison.")
        runtime_receipt(plan, dry, "rolled_back")
        if read_payload(fresh, "verification_json") != receipt:
            raise ValueError("The independently read sheets differ from the kept run.")
    elif compute(snapshot_payload(fresh)) != plan:
        raise ValueError("The fresh sheet inventory differs from the approved baseline.")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Snapshot or approved preview JSON")
    parser.add_argument("--run", type=Path, help="Full pyRevit run record")
    parser.add_argument("--fresh", type=Path, help="Independent read or kept verification record")
    parser.add_argument("--decision", choices=("rolled_back", "discarded", "committed"))
    parser.add_argument("--dry-run", type=Path, help="Full approved dry-run record before Keep")
    args = parser.parse_args(argv)
    try:
        if args.run or args.fresh or args.decision or args.dry_run:
            if not all((args.run, args.fresh, args.decision)):
                raise ValueError("Verification needs --run, --fresh, and --decision.")
            result = verify_outcome(load(args.source), load(args.run, envelope=True), load(args.fresh, envelope=True),
                                    args.decision, load(args.dry_run, envelope=True) if args.dry_run else None)
        else:
            result = compute(snapshot_payload(load(args.source, envelope=True)))
        print(encode(result))
    except (OSError, KeyError, TypeError, ValueError, RecursionError, AttributeError) as error:
        print(f"Cannot plan or verify sheet creation: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
