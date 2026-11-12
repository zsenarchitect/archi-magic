import json
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_library_discovery import manifest_digest
from family_parameter_transfer_preview import preview


def field(identifier, name, formula=None, **changes):
    return {"id": identifier, "name": name, "formula": formula, "formula_driven": formula is not None,
            "data_type": "length", "group": "geometry", "storage": "Double", "instance": False,
            "built_in": None, "shared_guid": None, "reporting": False, "read_only": False,
            "can_assign_formula": True, "yes_no": False, "associations": [], **changes}


def read(role, fields, types):
    record = {"kind": "family-parameter-transfer-read", "version": 1, "role": role, "revit_build": "fixture",
              "category_id": "-1000", "state": {
                  "definitions": {row["id"]: row for row in fields},
                  "types": {name: {key: {"state": "value", "value": value} for key, value in values.items()}
                            for name, values in types.items()},
                  "current_type": next(iter(types)),
                  "reference_scope": "family_document", "coverage": {"complete": True, "issues": []},
                  "dimensions": [],
              }}
    if role == "target":
        record.update(project={"project_uid": "fixture"}, loaded_family={"id": "20", "uid": "family-20"},
                      project_values={"types": [], "instances": []})
    return record


@pytest.fixture
def records():
    source = read("source", [
        field("1", "Base Width"), field("2", "Panel Width", "Base Width * 2"),
        field("3", "Total Width", "Panel Width + 1"),
    ], {"Narrow": {"1": 2.0, "2": 4.0, "3": 5.0}, "Wide": {"1": 3.0, "2": 6.0, "3": 7.0}})
    target = read("target", [field("10", "Base Width")],
                  {"Target A": {"10": 2.0}, "Target B": {"10": 3.0}})
    request = {"selected": ["Total Width", "Panel Width"], "type_map": {"Target A": "Narrow", "Target B": "Wide"}}
    return source, target, request


def seal(records):
    source, target, request = records
    source = dict(source, read_sha256=manifest_digest(source))
    target = dict(target, read_sha256=manifest_digest(target))
    request = dict(request, source_sha256=source["read_sha256"], target_sha256=target["read_sha256"])
    return source, target, request


def test_preview_cli_binds_exact_reads_and_orders_additions(records, tmp_path):
    source, target, request = seal(records)
    paths = [tmp_path / name for name in ("source.json", "target.json", "request.json")]
    wrapper = {"result": {"status": "ok", "snapshot_json": json.dumps(target)}}
    for path, data in zip(paths, (source, wrapper, request)):
        path.write_text(json.dumps(data))
    run = subprocess.run([sys.executable, str(SCRIPTS / "family_parameter_transfer_preview.py"),
                          "--source", str(paths[0]), "--target", str(paths[1]), "--request", str(paths[2])],
                         capture_output=True, text=True, check=True)
    result = json.loads(run.stdout)
    assert result["target_sha256"] == target["read_sha256"]
    assert result["source_sha256"] == source["read_sha256"]
    assert result["formula_order"] == ["Panel Width", "Total Width"]
    assert result["operations"]["Base Width"]["action"] == "retain"
    assert result["operations"]["Base Width"]["target_id"] == "10"
    assert result["operations"]["Total Width"]["expected_values"] == {
        "Target A": {"state": "value", "value": 5.0}, "Target B": {"state": "value", "value": 7.0}}
    assert result.pop("preview_sha256") == manifest_digest(result)


def test_existing_equivalent_selection_produces_zero_additions(records):
    source, target, request = records
    for key in ("2", "3"):
        new_key = str(10 + int(key))
        target["state"]["definitions"][new_key] = dict(source["state"]["definitions"][key], id=new_key)
        for target_type, source_type in request["type_map"].items():
            target["state"]["types"][target_type][new_key] = source["state"]["types"][source_type][key]
    result = preview(*seal(records))
    assert result["formula_order"] == []
    assert {name: row["action"] for name, row in result["operations"].items()} == {
        "Base Width": "retain", "Panel Width": "retain", "Total Width": "retain",
    }


def test_blocks_tampered_project_values(records):
    source, target, request = seal(records)
    target["project_values"]["types"].append({"changed": True})
    with pytest.raises(ValueError, match="differs from the reviewed read"):
        preview(source, target, request)


@pytest.mark.parametrize("change, reason", [
    ("partial", "reference inventory is incomplete"),
    ("missing_type", "Map every exact target type"),
    ("missing_dependency", "Select missing dependency"),
    ("case_conflict", "case-conflicting name"),
    ("spec_conflict", "existing target definition conflicts"),
    ("value_conflict", "retained value"),
    ("cycle", "contain a cycle"),
    ("unsupported_value", "unsupported, or invalid mapped value"),
    ("new_shared", "cannot add"),
    ("unset", "unset, unsupported"),
    ("boolean_range", "invalid mapped value"),
])
def test_blocks_ineligible_or_conflicting_transfer(records, change, reason):
    source, target, request = records
    source_state, target_state = source["state"], target["state"]
    if change == "partial":
        target_state["coverage"]["complete"] = False
    elif change == "missing_type":
        del request["type_map"]["Target A"]
    elif change == "missing_dependency":
        request["selected"].remove("Panel Width")
    elif change == "case_conflict":
        target_state["definitions"]["10"]["name"] = "base Width"
    elif change == "spec_conflict":
        target_state["definitions"]["10"]["data_type"] = "area"
    elif change == "value_conflict":
        target_state["types"]["Target A"]["10"]["value"] = 8.0
    elif change == "cycle":
        source_state["definitions"]["2"]["formula"] = "Total Width + 1"
    elif change == "unsupported_value":
        source_state["definitions"]["2"]["storage"] = "ElementId"
    elif change == "new_shared":
        source_state["definitions"]["2"]["shared_guid"] = "fixture"
    elif change == "unset":
        source_state["types"]["Wide"]["2"] = {"state": "unset"}
    elif change == "boolean_range":
        source_state["definitions"]["2"].update(storage="Integer", yes_no=True)
        source_state["types"]["Narrow"]["2"]["value"] = 2
    with pytest.raises(ValueError, match=reason):
        preview(*seal(records))


def test_final_namespace_rejects_a_new_ambiguous_formula_binding(records):
    _, target, _ = records
    target["state"]["definitions"]["14"] = field("14", "Base Width * 2")
    for values in target["state"]["types"].values():
        values["14"] = {"state": "value", "value": 12.0}
    with pytest.raises(ValueError, match="unsupported punctuation"):
        preview(*seal(records))
