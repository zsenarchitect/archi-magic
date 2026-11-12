"""Resolve exact dependencies for the supported Revit family arithmetic formulas."""

import argparse
from collections import Counter
import json
import re
import sys

from family_library_discovery import manifest_digest
from family_placement_preview import load_json

MAX_PARAMETERS = 250
MAX_SELECTED = 50
MAX_FORMULA_BYTES = 4096
MAX_TOKENS = 256
MAX_DEPTH = 32
NAME = re.compile(r"[^\W\d]\w*(?: +\w+)*")
DECIMAL = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
BINDING_POWER = {"+": (10, 11), "-": (10, 11), "*": (20, 21), "/": (20, 21), "^": (30, 30)}


def word_character(char):
    return char.isalnum() or char == "_"


def tokenize(formula, names):
    if (not isinstance(formula, str) or not formula.strip()
            or len(formula.encode("utf-8")) > MAX_FORMULA_BYTES
            or any(ord(char) < 32 and char != "\t" for char in formula)):
        raise ValueError("Use a nonempty single-line formula up to 4 KiB.")
    usable = []
    for name in names:
        if NAME.fullmatch(name):
            usable.append(name)
        elif re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", formula):
            raise ValueError(f"Formula reference {name!r} has unsupported punctuation or spacing.")
    usable.sort(key=len, reverse=True)
    counts = Counter(name.casefold() for name in names)
    tokens = []
    offset = 0
    while offset < len(formula):
        char = formula[offset]
        if char in " \t":
            offset += 1
            continue
        if len(tokens) >= MAX_TOKENS:
            raise ValueError("The formula exceeds 256 tokens.")
        if char in "+-*/^()":
            tokens.append((char, None))
            offset += 1
            continue
        number = DECIMAL.match(formula, offset)
        if number:
            tokens.append(("number", None))
            offset = number.end()
            continue
        for name in usable:
            end = offset + len(name)
            if formula.startswith(name, offset) and (end == len(formula) or not word_character(formula[end])):
                if counts[name.casefold()] != 1:
                    raise ValueError(f"Formula reference {name!r} has an ambiguous parameter identity.")
                tokens.append(("parameter", name))
                offset = end
                break
        else:
            raise ValueError(f"Unknown or unsupported formula token at character {offset + 1}.")
    return tokens


def formula_dependencies(formula, names):
    tokens = tokenize(formula, names) + [("end", None)]
    position = 0
    dependencies = set()

    def expression(minimum, depth):
        nonlocal position
        if depth > MAX_DEPTH:
            raise ValueError("The formula nesting exceeds 32 steps.")
        kind, value = tokens[position]
        position += 1
        if kind == "parameter":
            dependencies.add(value)
        elif kind in {"+", "-"}:
            expression(30, depth + 1)
        elif kind == "(":
            expression(0, depth + 1)
            if tokens[position][0] != ")":
                raise ValueError("The formula has an unclosed parenthesis.")
            position += 1
        elif kind != "number":
            raise ValueError("The formula needs an arithmetic operand.")
        while tokens[position][0] in BINDING_POWER:
            left, right = BINDING_POWER[tokens[position][0]]
            if left < minimum:
                break
            position += 1
            expression(right, depth + 1)

    expression(0, 0)
    if tokens[position][0] != "end":
        raise ValueError("The formula contains unsupported trailing input.")
    return sorted(dependencies)


def dependency_plan(definitions, selected):
    if (not isinstance(definitions, list) or not 1 <= len(definitions) <= MAX_PARAMETERS
            or not isinstance(selected, list) or not 1 <= len(selected) <= MAX_SELECTED
            or any(not isinstance(name, str) for name in selected) or len(set(selected)) != len(selected)):
        raise ValueError("Supply a bounded family definition inventory and distinct reviewed parameter names.")
    by_name = {}
    for field in definitions:
        name = field["name"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 4096 or name in by_name
                or (field["formula"] is not None and not isinstance(field["formula"], str))):
            raise ValueError("The family inventory has an invalid or duplicate parameter definition.")
        by_name[name] = field["formula"]
    if any(name not in by_name for name in selected):
        raise ValueError("A selected parameter is absent from the exact family inventory.")

    active, dependencies, depths, order = set(), {}, {}, []
    chosen = set(selected)

    def visit(name):
        if name in active:
            raise ValueError(f"The formula dependencies contain a cycle at {name!r}.")
        if name in depths:
            return depths[name]
        if len(active) > MAX_DEPTH:
            raise ValueError("The formula dependency chain exceeds 32 steps.")
        active.add(name)
        formula = by_name[name]
        references = formula_dependencies(formula, by_name) if formula is not None else []
        depth = max((visit(child) + 1 for child in references), default=0)
        if depth > MAX_DEPTH:
            raise ValueError("The formula dependency chain exceeds 32 steps.")
        active.remove(name)
        dependencies[name] = references
        depths[name] = depth
        if formula is not None and name in chosen:
            order.append(name)
        return depth

    for name in selected:
        visit(name)
    record = {"kind": "family-formula-dependencies", "version": 1, "selected": selected,
              "dependencies": dependencies, "formula_order": order}
    return dict(record, dependency_sha256=manifest_digest(record))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request")
    args = parser.parse_args()
    try:
        data = load_json(args.request)
        result = dependency_plan(data["definitions"], data["selected"])
        print(json.dumps(result, ensure_ascii=True, indent=2))
    except (AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
