"""Compare material quantity queries with a normalized Revit takeoff CSV."""
import argparse
import csv
from decimal import Decimal, DecimalException, localcontext
import json
import re
import sys
from pathlib import Path

from carbon_report import number, table

MAX_INPUT = 8 * 1024 * 1024
MAX_BATCHES = 32
MAX_DETAIL_ROWS = 50000
NAMES = ("category", "level", "material")
MEASURES = ("area_m2", "volume_m3")
ZERO = Decimal(0)


class Incomplete(ValueError):
    def __init__(self, message, issues=()):
        super().__init__(message)
        self.issues = list(issues)


def read_payload(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError("input exceeds 8 MiB")
    report = json.loads(data.decode("utf-8-sig"), parse_float=Decimal)
    if not isinstance(report, dict):
        raise ValueError("expected a query object")
    if "response" in report:
        report = report["response"]
        if not isinstance(report, dict):
            raise ValueError("expected a saved runtime response")
    if {"result", "mode", "result_truncated", "changes"}.intersection(report):
        exit_code = report.get("exit_code", 0)
        if (report.get("status", "ok") not in ("ok", "success", "succeeded", "completed")
                or type(exit_code) is not int or exit_code != 0
                or report.get("error") or report.get("mode", "query") != "query"
                or report.get("result_truncated", False) is not False or report.get("failures") or report.get("blocked")
                or report.get("unclosed_documents")):
            raise Incomplete("save the complete successful read-only query response")
        if "changes" in report:
            changes = report["changes"]
            if (not isinstance(changes, dict)
                    or any(type(changes.get(key)) is not int or changes[key] != 0
                           for key in ("added_count", "modified_count", "deleted_count"))
                    or any(changes.get(key) for key in ("added", "modified", "deleted", "added_ids", "modified_ids",
                                                        "deleted_ids", "by_category", "other_documents"))):
                raise Incomplete("the saved query does not verify zero model changes")
        report = report.get("result")
    if not isinstance(report, dict) or report.get("error"):
        raise Incomplete("the query did not return a report")
    if "payload" in report:
        report = json.loads(report["payload"], parse_float=Decimal)
    if not isinstance(report, dict):
        raise ValueError("expected a decoded report object")
    return report


def element_ids(report):
    ids = report.get("requested_element_ids")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 500 or type(report.get("requested_elements")) is not int:
        raise Incomplete("rerun the quantity query to record its requested element IDs")
    if any(not isinstance(item, str) or not re.fullmatch("[1-9][0-9]{0,18}", item)
           or int(item) >= 2 ** 63 for item in ids):
        raise ValueError("invalid requested element ID")
    if len(ids) != report["requested_elements"] or len(set(ids)) != len(ids):
        raise ValueError("requested element count or uniqueness does not match the query")
    return set(ids)


def quantity(value, line, field):
    value = number(value, line, field)
    if value < ZERO or value >= Decimal("1e18") or value.as_tuple().exponent < -24:
        raise ValueError(f"line {line} has an out-of-range {field}")
    return value


def add(groups, key, amounts):
    total = groups.setdefault(key, [ZERO, ZERO])
    for index, value in enumerate(amounts):
        total[index] += value


def query_groups(paths):
    groups, seen, identity_to_name, name_to_identity = {}, set(), {}, {}
    project = None
    for path in paths:
        report = read_payload(path)
        issues = report.get("issues")
        if not isinstance(issues, list):
            raise ValueError("query issues must be a list")
        if report.get("status") != "reported" or issues:
            raise Incomplete("a material quantity query is incomplete", issues or [])
        stamp = report.get("project")
        if (not isinstance(stamp, dict) or not isinstance(stamp.get("uid"), str) or not stamp["uid"]
                or not isinstance(stamp.get("path_sha256"), str)
                or not re.fullmatch("[0-9a-f]{64}", stamp["path_sha256"])):
            raise Incomplete("rerun the material quantity query in a saved project")
        if project is not None and stamp != project:
            raise ValueError("queries come from different saved projects")
        project = stamp
        ids = element_ids(report)
        if ids & seen:
            raise ValueError("query batches overlap")
        seen.update(ids)
        rows = report.get("rows")
        if not isinstance(rows, list) or not rows:
            raise Incomplete("a material quantity query has no rows")
        if len(rows) > 5000:
            raise ValueError("query exceeds the material-pair limit")
        batch = set()
        for index, row in enumerate(rows, 1):
            names, identifiers = [], []
            for field in NAMES:
                label, identifier = row[field], row[field + "_id"]
                if (not isinstance(label, str) or not label.strip() or type(identifier) is not int
                        or identifier == 0 or (field != "category" and identifier < 0)):
                    raise ValueError(f"query row {index} has an invalid {field} name or ID")
                label = label.strip()
                if (identity_to_name.setdefault((field, identifier), label) != label
                        or name_to_identity.setdefault((field, label), identifier) != identifier):
                    raise ValueError(f"ambiguous {field} name or ID across query rows")
                names.append(label)
                identifiers.append(identifier)
            key = tuple(identifiers)
            if key in batch:
                raise ValueError("duplicate grouped material row in a query batch")
            batch.add(key)
            add(groups, tuple(names), [quantity(row[field], index, field) for field in MEASURES])
    return groups, len(seen)


def schedule_groups(path):
    if path.stat().st_size > MAX_INPUT:
        raise ValueError("input exceeds 8 MiB")
    groups = {}
    for index, (line, row) in enumerate(table(path, NAMES + MEASURES), 1):
        if index > MAX_DETAIL_ROWS:
            raise ValueError("schedule exceeds 50,000 detail rows")
        add(groups, tuple(row[field] for field in NAMES), [quantity(row[field], line, field) for field in MEASURES])
    if not groups:
        raise Incomplete("the schedule has no material detail rows")
    return groups


def compare_quantity(actual, reference):
    within = None
    percent = None
    if actual is not None and reference is not None:
        difference = abs(actual - reference)
        within = difference * 100 <= reference
        if reference:
            percent = format(difference * 100 / reference, ".6f")
    return {"query": str(actual) if actual is not None else None,
            "schedule": str(reference) if reference is not None else None,
            "difference_percent": percent, "within_tolerance": within is True}


def compare_groups(actual, reference, fields):
    result = []
    for key in sorted(set(actual) | set(reference)):
        row = dict(zip(fields, key))
        found, expected = actual.get(key, (None, None)), reference.get(key, (None, None))
        for index, measure in enumerate(MEASURES):
            row[measure] = compare_quantity(found[index], expected[index])
        row["status"] = ("schedule_only" if key not in actual else
                         "query_only" if key not in reference else
                         "match" if all(row[name]["within_tolerance"] for name in MEASURES) else "difference")
        result.append(row)
    return result


def by_level(groups):
    result = {}
    for (_, level, _), values in groups.items():
        add(result, (level,), values)
    return result


def validate_schedule(path, schedule_id):
    record = read_payload(path)
    if record.get("kind") != "schedules":
        raise ValueError("expected the original schedule read response")
    schedules = [row for row in record["schedules"] if str(row["id"]) == schedule_id]
    if len(schedules) != 1 or len(record["schedules"]) != 1 or record.get("missing"):
        raise Incomplete("read exactly the selected schedule ID")
    schedule = schedules[0]
    if schedule.get("kind") != "material takeoff":
        raise ValueError("the selected schedule is not a material takeoff")
    rows, kinds = schedule["rows"], schedule["rowKinds"]
    if (schedule.get("truncated") is not False or schedule.get("itemized") is not True
            or len(rows) != len(kinds) or len(rows) != schedule.get("totalRows")
            or any(kind not in ("blank", "body") for kind in kinds) or "body" not in kinds):
        raise Incomplete("the material takeoff must be complete, itemized and free of group or total rows")
    return {"status": "ready_for_export", "id": schedule_id, "category": schedule["category"],
            "detail_rows": kinds.count("body")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("--query", type=Path, action="append", required=True)
    compare.add_argument("--schedule", type=Path, required=True)
    validate = commands.add_parser("validate-schedule")
    validate.add_argument("record", type=Path)
    validate.add_argument("schedule_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-schedule":
            result = validate_schedule(args.record, args.schedule_id)
        else:
            if len(args.query) > MAX_BATCHES:
                raise ValueError("provide up to 32 query batches")
            with localcontext() as ctx:
                ctx.prec = 60
                actual, count = query_groups(args.query)
                expected = schedule_groups(args.schedule)
                groups = compare_groups(actual, expected, NAMES)
                levels = compare_groups(by_level(actual), by_level(expected), ("level",))
            passed = all(row["status"] == "match" for row in groups + levels)
            result = {"status": "match" if passed else "mismatch", "queried_elements": count,
                      "groups": groups, "levels": levels}
    except Incomplete as exc:
        print(json.dumps({"status": "incomplete", "reason": str(exc), "issues": exc.issues}, indent=2, default=str))
        return 1
    except (OSError, ValueError, TypeError, KeyError, csv.Error, DecimalException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in ("match", "ready_for_export") else 1


if __name__ == "__main__":
    raise SystemExit(main())
