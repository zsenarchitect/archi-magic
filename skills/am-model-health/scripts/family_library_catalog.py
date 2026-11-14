"""Read candidate names from a Revit type catalog. Revit supplies the typed values."""

from collections import Counter
import csv

MAX_CATALOG_LINES = 20_000
MAX_TYPE_NAME = 4096
MAX_ISSUES = 100


def catalog_candidates(data, embedded_names, limit):
    try:
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        lines = data.decode(encoding).splitlines()
    except UnicodeError:
        return [], sorted(embedded_names), [{"reason": "unsupported_encoding"}]
    if not lines or not lines[0] or any("\x00" in line for line in lines) or len(lines) > MAX_CATALOG_LINES:
        return [], sorted(embedded_names), [{"reason": "invalid_catalog_or_line_limit"}]
    delimiter = lines[0][0]
    if delimiter.isalnum() or delimiter == '"' or (delimiter.isspace() and delimiter != "\t"):
        return [], sorted(embedded_names), [{"reason": "invalid_delimiter"}]

    def fields(line):
        return next(csv.reader([line], delimiter=delimiter, strict=True))

    try:
        header = fields(lines[0])
    except csv.Error:
        return [], sorted(embedded_names), [{"reason": "invalid_header"}]
    definitions = [field.split("##") for field in header[1:]]
    if (header[0] or not definitions or any(len(field) != 3 or not field[0] or not field[1] for field in definitions)
            or len({field[0] for field in definitions}) != len(definitions)):
        return [], sorted(embedded_names), [{"reason": "invalid_header"}]

    issues = []
    rows = []
    ambiguous_names = False

    def issue(line, reason):
        nonlocal ambiguous_names
        if reason in {"invalid_type_row", "invalid_type_name_quotes", "invalid_type_name"}:
            ambiguous_names = True
        if len(issues) < MAX_ISSUES:
            issues.append({"line": line, "reason": reason})

    for number, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        try:
            row = fields(line)
        except csv.Error:
            issue(number, "invalid_type_row")
            continue
        name = row[0]
        if not line.startswith('"'):
            if '"' in name.replace('""', ""):
                issue(number, "invalid_type_name_quotes")
                continue
            name = name.replace('""', '"')
        # Keep the name of a malformed row for collision checks.
        if not name.strip() or len(name) > MAX_TYPE_NAME or any(ord(char) < 32 for char in name):
            issue(number, "invalid_type_name")
            continue
        valid = len(row) == len(header)
        if not valid:
            issue(number, "wrong_value_count")
        rows.append((number, name, valid))

    counts = Counter(name.casefold() for _, name, _ in rows)
    embedded = {name.casefold() for name in embedded_names}
    blocked = {name for name in embedded_names if name.casefold() in counts}
    candidates = []
    for number, name, valid in rows:
        key = name.casefold()
        if counts[key] != 1 or key in embedded:
            issue(number, "type_name_collision")
        elif valid and len(candidates) < limit:
            candidates.append(name)
        elif valid:
            issue(number, "type_limit")
    return candidates, sorted(embedded_names if ambiguous_names else blocked), issues
