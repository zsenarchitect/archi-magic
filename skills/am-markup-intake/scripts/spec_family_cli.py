"""Prepare and confirm a local PDF specification for family authoring."""

import argparse
from decimal import Decimal
import hashlib
import json
import re
import sys

from document_tables import MAX_PDF_PAGE, local_bytes, pdf_rows

LIMIT = 128 * 1024
DIMENSIONS = ("width", "depth", "height")
SIDES = (("left", "right"), ("front", "back"), ("below", "above"))
CLEARANCES = tuple("clearance_" + side for pair in SIDES for side in pair)
FACTORS = {"mm": Decimal(1), "cm": Decimal(10), "m": Decimal(1000),
           "in": Decimal("25.4"), "ft": Decimal("304.8")}
LENGTH = re.compile(
    r"""(?:([0-9]{1,8}(?:\.[0-9]{1,6})?)|(?:([0-9]{1,8})(?: +|-))?([0-9]{1,3})/([0-9]{1,3}))"""
    r"""\s*(mm|cm|m|in|ft|["'′″])?\Z""", re.ASCII,
)
UNIT_MARKS = {'"': "in", "″": "in", "'": "ft", "′": "ft"}
TEXT_FIELD = re.compile(r"(?:connector|parameter)_[a-z][a-z0-9_]{0,39}\Z")
PREVIEW = "spec-family-preview"
DECISIONS = "spec-family-decisions"


def encode(value):
    data = (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode()
    if len(data) > LIMIT:
        raise ValueError("Specification exceeds the 128 KiB report limit.")
    return data


def unique_keys(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("JSON contains a duplicate field.")
        result[name] = value
    return result


def read_json(path):
    _, data = local_bytes(path)
    if len(data) > LIMIT:
        raise ValueError("Specification inputs must be at most 128 KiB.")
    return data, json.loads(data, object_pairs_hook=unique_keys)


def text_value(value, maximum):
    if (
        not isinstance(value, str) or not value.strip() or len(value) > maximum
        or not value.isprintable()
    ):
        raise ValueError("Use a single line of printable text within the field limit.")
    return value.strip()


def positive_index(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"Use a whole-number table position between 1 and {maximum}.")
    return value


def length_mm(raw, unit, *, clearance):
    match = LENGTH.fullmatch(raw)
    if not match:
        raise ValueError("Use a decimal or fractional length with mm, cm, m, in or ft.")
    number, whole, numerator, denominator, cell_unit = match.groups()
    cell_unit = UNIT_MARKS.get(cell_unit, cell_unit)
    if cell_unit is not None and unit is not None and cell_unit != unit:
        raise ValueError("The mapped unit conflicts with the PDF cell.")
    chosen = cell_unit or unit
    if chosen not in FACTORS:
        raise ValueError("Map an explicit source unit for this numeric cell.")
    if numerator is None:
        value = Decimal(number) * FACTORS[chosen]
    else:
        top, bottom = int(numerator), int(denominator)
        if bottom == 0 or (whole is not None and top >= bottom):
            raise ValueError("Use a nonzero denominator and a proper fraction after a whole number.")
        value = (Decimal(whole or 0) + Decimal(top) / bottom) * FACTORS[chosen]
    if not (0 if clearance else 1) <= value <= 30000:
        raise ValueError("A physical length is outside its supported range.")
    if numerator is not None:
        value = value.quantize(Decimal("0.000000000001"))
    return format(value.normalize(), "f")


def build_preview(pdf, mapping, *, expected_pdf=None):
    if set(mapping) != {"product", "variant", "fields"}:
        raise ValueError("A mapping needs product, variant and fields.")
    product = text_value(mapping["product"], 160)
    variant = text_value(mapping["variant"], 160)
    fields = mapping["fields"]
    if not isinstance(fields, list) or not 3 <= len(fields) <= 32:
        raise ValueError("Map 3 to 32 fields for one product variant.")

    plan = {}
    for item in fields:
        if not {"name", "page", "table", "row", "column"} <= set(item) <= {
            "name", "page", "table", "row", "column", "unit",
        }:
            raise ValueError("Each field needs a name and a one-based PDF cell position.")
        name = item["name"]
        numeric = name in DIMENSIONS or name in CLEARANCES
        if name in plan or not (numeric or TEXT_FIELD.fullmatch(name)):
            raise ValueError("Use distinct dimension, clearance, connector or parameter fields.")
        if "unit" in item and (not numeric or item["unit"] not in FACTORS):
            raise ValueError("Use an explicit supported unit on a numeric field only.")
        position = {
            key: positive_index(item[key], maximum)
            for key, maximum in (("page", MAX_PDF_PAGE), ("table", 64), ("row", 5000), ("column", 128))
        }
        plan[name] = (position, item.get("unit"), numeric)

    if not set(DIMENSIONS) <= plan.keys():
        raise ValueError("Width, depth and height are required.")
    clearance_names = set(CLEARANCES) & plan.keys()
    if clearance_names and clearance_names != set(CLEARANCES):
        raise ValueError("Map all six directional clearance sides, including any zero offsets.")
    if not clearance_names and not any(name.startswith("connector_") for name in plan):
        raise ValueError("Map either directional clearance or a connector specification.")

    table_keys = dict.fromkeys((p["page"], p["table"]) for p, _, _ in plan.values())
    if len(table_keys) > 8:
        raise ValueError("Select at most eight PDF tables.")
    source, data = local_bytes(pdf)
    if source.suffix.lower() != ".pdf":
        raise ValueError("Select a local product PDF.")
    pdf_info = {"file": text_value(source.name, 250), "sha256": hashlib.sha256(data).hexdigest()}
    if expected_pdf is not None and expected_pdf != pdf_info:
        raise ValueError("The PDF has changed. Generate and review a new preview.")
    tables = {key: dict(pdf_rows(data, *key)) for key in table_keys}
    rows = []
    for index, (name, (position, unit, numeric)) in enumerate(plan.items(), 1):
        table = tables[position["page"], position["table"]]
        cells = table.get(position["row"], [])
        column = position["column"]
        if column > len(cells):
            raise ValueError(f"The mapped cell for {name} does not exist.")
        raw = text_value(cells[column - 1], 500)
        try:
            value = length_mm(raw, unit, clearance=name in CLEARANCES) if numeric else raw
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from exc
        rows.append({"id": index, "name": name, "source": position, "extracted_text": raw,
                     "value": value, "unit": "mm" if numeric else None, "mapped_unit": unit})

    values = {row["name"]: row["value"] for row in rows}
    for axis, (before, after) in zip(DIMENSIONS, SIDES):
        if clearance_names and sum(Decimal(values[name]) for name in (
            axis, "clearance_" + before, "clearance_" + after,
        )) > 60000:
            raise ValueError(f"The clearance envelope along {axis} exceeds 60,000 mm.")
    return {
        "kind": PREVIEW, "status": "needs_confirmation", "product": product, "variant": variant,
        "pdf": pdf_info, "rows": rows,
    }


def template(preview, data):
    return {
        "kind": DECISIONS,
        "preview_sha256": hashlib.sha256(data).hexdigest(),
        "decisions": [{"id": row["id"], "action": ""} for row in preview["rows"]],
    }


def confirm(pdf, mapping, preview_path, decisions_path):
    preview_bytes, preview = read_json(preview_path)
    _, decisions = read_json(decisions_path)
    expected = template(preview, preview_bytes)
    if (
        set(decisions) != set(expected) or decisions["kind"] != DECISIONS
        or decisions["preview_sha256"] != expected["preview_sha256"]
    ):
        raise ValueError("Decisions must identify the exact preview bytes.")
    expected["decisions"] = [{"id": row["id"], "action": "accept"} for row in preview["rows"]]
    choices = decisions["decisions"]
    if choices != expected["decisions"] or any(type(choice["id"]) is not int for choice in choices):
        raise ValueError("Explicitly accept every row in order. Correct the mapping and preview again if needed.")
    if preview != build_preview(pdf, mapping, expected_pdf=preview['pdf']):
        raise ValueError("The PDF, mapping or preview has changed. Generate and review a new preview.")

    values = {row["name"]: row["value"] for row in preview["rows"]}
    return {
        "kind": "confirmed-family-specification", "model_write_approved": False,
        "product": preview["product"], "variant": preview["variant"], "pdf": preview["pdf"],
        "preview_sha256": expected["preview_sha256"], "source_rows": preview["rows"],
        "dimensions_mm": {name: values[name] for name in DIMENSIONS},
        "clearance_mm": {name: values[name] for name in CLEARANCES if name in values},
        "connectors": {name: value for name, value in values.items() if name.startswith("connector_")},
        "parameters": {name: value for name, value in values.items() if name.startswith("parameter_")},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("preview", "template", "confirm"):
        command = subcommands.add_parser(name)
        if name != "template":
            command.add_argument("pdf")
            command.add_argument("--mapping", required=True)
        if name != "preview":
            command.add_argument("--preview", required=True)
        if name == "confirm":
            command.add_argument("--decisions", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "template":
            data, preview = read_json(args.preview)
            if preview["kind"] != PREVIEW:
                raise ValueError("Select a family specification preview.")
            result = template(preview, data)
        else:
            _, mapping = read_json(args.mapping)
            result = (
                build_preview(args.pdf, mapping) if args.command == "preview"
                else confirm(args.pdf, mapping, args.preview, args.decisions)
            )
        sys.stdout.write(encode(result).decode("ascii"))
    except (ValueError, KeyError, TypeError, OSError, RecursionError) as exc:
        print(f"Specification review failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
