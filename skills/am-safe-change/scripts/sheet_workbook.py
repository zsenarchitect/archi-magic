"""Prepare a Revit sheet request from a local workbook or recheck an approved request."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

import sheet_creation_preview as preview


def workbook_value(cell, kind):
    if kind == "string":
        return cell
    if kind == "yes_no":
        boolean = {"true": True, "yes": True, "false": False, "no": False}
        if cell.lower() not in boolean:
            raise ValueError("Use Yes or No for a workbook yes/no value.")
        return boolean[cell.lower()]
    if kind in ("integer", "double", "element_id"):
        return json.loads(cell)
    raise ValueError("The workbook parameter storage kind is unsupported.")


def prepare(workbook, mapping):
    preview.fields(mapping, ("sheet", "header_row", "columns", "title_block_types"), ("parameters",))
    worksheet = preview.text(mapping["sheet"])
    header = mapping["header_row"]
    if type(header) is not int or not 1 <= header <= 5000:
        raise ValueError("Choose a valid worksheet header row.")
    columns = mapping["columns"]
    preview.fields(columns, ("number", "name", "title_block"))
    for column in columns.values():
        preview.name(column)
    if len(set(columns.values())) != len(columns):
        raise ValueError("Map the number, name, and title block to distinct columns.")
    types = mapping["title_block_types"]
    if not isinstance(types, dict) or not 1 <= len(types) <= preview.MAX_TYPES:
        raise ValueError("Map title block labels to reviewed Revit type IDs.")
    for label, type_id in types.items():
        preview.name(label)
        preview.element_id(type_id)
    assignments = preview.bounded(mapping.get("parameters", []), preview.MAX_PARAMETERS)
    for entry in assignments:
        preview.fields(entry, ("column", "scope", "parameter", "kind"), ("unit",))
        preview.name(entry["column"])
    if len({entry["column"] for entry in assignments}) != len(assignments):
        raise ValueError("Each workbook parameter needs a distinct column.")

    # Both skills ship in the archi-magic plugin. Use the bounded local extractor.
    scripts = Path(__file__).resolve().parents[2] / "am-markup-intake" / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        from document_tables import extract
    finally:
        sys.path.pop(0)
    table = extract(str(workbook), sheet=worksheet, header=header, literal_text_column=columns["number"])
    headings = set(table["columns"])
    if not set(columns.values()).union(entry["column"] for entry in assignments) <= headings:
        raise ValueError("A mapped column is missing from the selected worksheet.")

    rows = []
    for row in preview.bounded(table["rows"], preview.MAX_ROWS):
        line = row["source_row"]
        cells = dict(zip(table["columns"], row["cells"]))
        try:
            type_id = types.get(cells[columns["title_block"]])
            if type_id is None:
                raise ValueError("The title block label has no reviewed type mapping.")
            parameters = []
            for entry in assignments:
                cell = cells[entry["column"]]
                if cell:
                    assignment = {key: value for key, value in entry.items() if key != "column"}
                    assignment["value"] = workbook_value(cell, entry["kind"])
                    parameters.append(preview.normalize_assignment(assignment))
            rows.append({"number": cells[columns["number"]], "name": cells[columns["name"]],
                         "title_block_type_id": type_id, "source_row": line, "parameters": parameters})
        except ValueError as error:
            raise ValueError(f"Workbook row {line}: {error}") from error

    source = {key: table["source"][key] for key in ("file", "sheet", "header_row", "sha256")}
    source["mapping"] = columns
    source["mapping_sha256"] = hashlib.sha256(preview.encode(mapping).encode("ascii")).hexdigest()
    return preview.normalize_request({"source": source, "rows": rows})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("mapping", type=Path, help="Reviewed worksheet, columns, and title block mappings")
    parser.add_argument("--check", type=Path, help="Approved sheet creation preview JSON")
    args = parser.parse_args(argv)
    try:
        request = prepare(args.workbook, preview.load(args.mapping))
        if args.check:
            plan = preview.load(args.check)
            if plan.get("recipe") != "sheet-creation" or plan["request"] != request:
                raise ValueError("The workbook, mapping, or sheet rows changed after review.")
            print("The workbook and mapping match the reviewed sheet request.")
        else:
            print(preview.encode({"request": request}))
    except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        print(f"Cannot prepare or verify sheet workbook: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
