"""Plan exact family parameter additions without opening or modifying a Revit model."""

import argparse
import json
import math
import sys

from family_formula_dependencies import MAX_PARAMETERS, dependency_plan
from family_library_discovery import manifest_digest
from family_placement_preview import load_json
from read_family_library import MAX_TYPES

DEFINITION_FIELDS = ("name", "data_type", "group", "storage", "instance", "built_in", "shared_guid",
                     "reporting", "formula", "yes_no")


def reviewed_snapshot(record, role, expected_digest):
    record = record.get("result", record)
    if "snapshot_json" in record:
        if record.get("status") != "ok":
            raise ValueError(f"The {role} inventory is incomplete.")
        record = json.loads(record["snapshot_json"])
    body = dict(record)
    digest = body.pop("read_sha256")
    if (digest != expected_digest or manifest_digest(body) != digest
            or body["kind"] != "family-parameter-transfer-read" or body["version"] != 1
            or body["role"] != role):
        raise ValueError(f"The {role} inventory differs from the reviewed read.")
    state = body["state"]
    if (state["coverage"] != {"complete": True, "issues": []}
            or state["reference_scope"] != "family_document"):
        raise ValueError(f"The {role} family reference inventory is incomplete.")
    return record


def definitions_by_name(state):
    definitions = state["definitions"]
    if not 1 <= len(definitions) <= MAX_PARAMETERS:
        raise ValueError("The family definition inventory exceeds the supported bounds.")
    by_name = {}
    folded = set()
    for key, field in definitions.items():
        name = field["name"]
        if key != field["id"] or name.casefold() in folded:
            raise ValueError(f"The family contains an ambiguous definition for {name!r}.")
        if field["formula_driven"] != (field["formula"] is not None):
            raise ValueError(f"The inventory has inconsistent formula evidence for {name!r}.")
        by_name[name] = field
        folded.add(name.casefold())
    return by_name


def mapped_types(source, target, mapping):
    for state in (source, target):
        types = state["types"]
        if (not 1 <= len(types) <= MAX_TYPES or state["current_type"] not in types
                or any(values.keys() != state["definitions"].keys() for values in types.values())):
            raise ValueError("Both families require a current type and complete per-type values.")
    if (not isinstance(mapping, dict) or mapping.keys() != target["types"].keys()
            or any(name not in source["types"] for name in mapping.values())):
        raise ValueError("Map every exact target type name to an exact source type name.")


def reviewed_value(field, value):
    storage = field["storage"]
    raw = value.get("value")
    valid = (
        (storage == "String" and isinstance(raw, str))
        or (storage == "Double" and type(raw) is float and math.isfinite(raw))
        or (storage == "Integer" and type(raw) is int and -(2 ** 31) <= raw < 2 ** 31
            and (not field["yes_no"] or raw in (0, 1)))
    )
    if value.get("state") != "value" or not valid:
        raise ValueError(f"{field['name']!r} has an unset, unsupported, or invalid mapped value.")
    return value


def preview(source_read, target_read, request):
    source_read = reviewed_snapshot(source_read, "source", request["source_sha256"])
    target_read = reviewed_snapshot(target_read, "target", request["target_sha256"])
    if (source_read["category_id"] != target_read["category_id"]
            or source_read["revit_build"] != target_read["revit_build"]):
        raise ValueError("Use the same family category and Revit build for both reads.")

    source, target = source_read["state"], target_read["state"]
    mapping = request["type_map"]
    mapped_types(source, target, mapping)
    source_fields, target_fields = definitions_by_name(source), definitions_by_name(target)
    selected = request["selected"]
    source_graph = dependency_plan(list(source_fields.values()), selected)
    selected_set = set(selected)
    target_folded = {name.casefold() for name in target_fields}
    future_fields = dict(target_fields)
    operations = {}

    for name in source_graph["dependencies"]:
        field = source_fields[name]
        retained = target_fields.get(name)
        definition = {key: field[key] for key in DEFINITION_FIELDS}
        if retained is None:
            if name.casefold() in target_folded:
                raise ValueError(f"The target has a case-conflicting name for {name!r}.")
            if name not in selected_set:
                raise ValueError(f"Select missing dependency {name!r} or use a compatible existing definition.")
            if (field["instance"] or field["read_only"] or field["reporting"]
                    or field["shared_guid"] is not None or field["built_in"] is not None
                    or (field["formula"] is not None and not field["can_assign_formula"])):
                raise ValueError(f"The initial transfer cannot add {name!r} with this definition.")
            future_fields[name] = field
        elif any(retained[key] != value for key, value in definition.items()):
            raise ValueError(f"The existing target definition conflicts with {name!r}.")

        if name in selected_set and field["instance"]:
            raise ValueError(f"The initial transfer only supports selected type definitions, including {name!r}.")
        expected = {}
        for target_type, source_type in mapping.items():
            value = reviewed_value(field, source["types"][source_type][field["id"]])
            if retained is not None:
                actual = reviewed_value(retained, target["types"][target_type][retained["id"]])
                if actual != value:
                    raise ValueError(f"The retained value for {name!r} conflicts in target type {target_type!r}.")
            expected[target_type] = value
        operations[name] = {
            "action": "retain" if retained is not None else "add",
            "selected": name in selected_set,
            "source_id": field["id"],
            "target_id": retained["id"] if retained is not None else None,
            "definition": definition,
            "expected_values": expected,
        }

    target_graph = dependency_plan(list(future_fields.values()), selected)
    if target_graph["dependencies"] != source_graph["dependencies"]:
        raise ValueError("The final target namespace changes a formula dependency.")
    record = {
        "kind": "family-parameter-transfer-preview",
        "version": 1,
        "source_sha256": source_read["read_sha256"],
        "target_sha256": target_read["read_sha256"],
        "project": target_read["project"],
        "loaded_family": target_read["loaded_family"],
        "revit_build": target_read["revit_build"],
        "category_id": target_read["category_id"],
        "type_map": mapping,
        "operations": operations,
        "formula_order": [name for name in target_graph["formula_order"] if operations[name]["action"] == "add"],
    }
    return dict(record, preview_sha256=manifest_digest(record))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    try:
        result = preview(load_json(args.source), load_json(args.target), load_json(args.request))
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Transfer preview blocked. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
