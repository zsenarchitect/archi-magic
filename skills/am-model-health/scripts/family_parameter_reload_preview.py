"""Bind sealed family transfers to one exact saved Revit project before reload."""

import argparse
import json
import sys
from uuid import uuid4

from batch_family_type_preview import validate_record
from family_library_discovery import MAX_FAMILY_BYTES, MAX_TOTAL_BYTES, manifest_digest
from family_placement_preview import load_json
from family_transfer_nested import nested_names
from read_family_library import WorkerUnresolved, bounded_text
from read_family_parameter_transfer import read_result
from read_family_placement import MAX_FAMILIES, eid, inventory, inventory_record, saved_project_identity

MAX_TARGETS = 20


def project_families(document, DB):
    names = {}
    for family in DB.FilteredElementCollector(document).OfClass(DB.Family):
        identifier = eid(family)
        if identifier in names or len(names) >= MAX_FAMILIES:
            raise ValueError("The complete loaded project family inventory exceeds its limit.")
        names[identifier] = {"uid": bounded_text(family.UniqueId), "name": bounded_text(family.Name)}
    return names


def read_baseline(ui, DB, document, request):
    targets = request["targets"]
    if not 1 <= len(targets) <= MAX_TARGETS:
        raise ValueError("Select between one and twenty reviewed loaded families.")
    ids = [target["id"] for target in targets]
    if len(ids) != len(set(ids)):
        raise ValueError("Select each loaded project family only once.")
    project = saved_project_identity(document)
    elements = inventory(document, DB)
    families = project_families(document, DB)
    reads = []
    for target in targets:
        read = read_result(ui, DB, document, {"role": "target", "revit_build": request["revit_build"],
                                            "family_id": target["id"], "family_uid": target["uid"]})
        if read["state"]["coverage"] != {"complete": True, "issues": []}:
            raise ValueError("A loaded target has incomplete reference evidence.")
        reads.append(read)
    if (saved_project_identity(document) != project or inventory(document, DB) != elements
            or project_families(document, DB) != families):
        raise WorkerUnresolved("The project changed during the read-only reload baseline.")
    body = {"kind": "family-transfer-reload-baseline", "version": 1, "project": project,
            "revit_build": request["revit_build"], "families": families, "inventory": inventory_record(elements),
            "targets": reads}
    encoded = json.dumps(body, ensure_ascii=True, allow_nan=False)
    if len(encoded) > 8 * 1024 * 1024:
        raise ValueError("The project reload baseline exceeds the saved record size limit.")
    return dict(body, baseline_sha256=manifest_digest(body))


def baseline_record(record):
    record = record.get("response", record)
    record = record.get("result", record)
    if "snapshot_json" in record:
        if record.get("status") != "ok":
            raise ValueError("Read a complete saved project baseline first.")
        record = json.loads(record["snapshot_json"])
    validate_record(record, "family-transfer-reload-baseline", "baseline_sha256")
    return record


def build_preview(baseline, receipts):
    baseline = baseline_record(baseline)
    targets = baseline["targets"]
    if not 1 <= len(targets) == len(receipts) <= MAX_TARGETS:
        raise ValueError("Supply one sealed export for every reviewed project target.")
    exports = {}
    total_bytes = 0
    for receipt in receipts:
        validate_record(receipt, "family-transfer-derived-rfa", "derived_sha256")
        byte_count = receipt["rfa"]["bytes"]
        if type(byte_count) is not int or not 0 < byte_count <= MAX_FAMILY_BYTES:
            raise ValueError("The sealed family size is outside the reviewed limits.")
        total_bytes += byte_count
        if total_bytes > MAX_TOTAL_BYTES:
            raise ValueError("The sealed reload batch exceeds 4 GiB.")
        identifier = receipt["loaded_family"]["id"]
        if identifier in exports:
            raise ValueError("The sealed exports include a repeated loaded family.")
        exports[identifier] = receipt
    families = baseline["families"]
    names = {family["name"].casefold() for family in families.values()}
    rows = []
    for target in targets:
        validate_record(target, "family-parameter-transfer-read", "read_sha256")
        identity = target["loaded_family"]
        identifier = identity["id"]
        receipt = exports.pop(identifier, None)
        name = target["family"]
        if (receipt is None or receipt["target_sha256"] != target["read_sha256"]
                or target["role"] != "target" or identity != receipt["loaded_family"]
                or target["state"]["coverage"] != {"complete": True, "issues": []}
                or families.get(identifier) != {"uid": identity["uid"], "name": name}
                or any(target[key] != receipt[key] for key in ("project", "family", "category_id", "revit_build"))
                or target["project"] != baseline["project"] or target["revit_build"] != baseline["revit_build"]):
            raise ValueError("A sealed export differs from its exact reviewed loaded family.")
        if sum(family["name"].casefold() == name.casefold() for family in families.values()) != 1:
            raise ValueError("The loaded top-level family name is ambiguous in the project.")
        if receipt["nested_families"] != target["nested_families"]:
            raise ValueError("The sealed export changed the reviewed nested family inventory.")
        if nested_names(receipt["nested_families"]) & names:
            raise ValueError("A nested family name collides with a loaded project family.")
        rows.append({"loaded_family": identity, "name": name, "receipt": receipt})
    result = {"kind": "family-transfer-reload-preview", "version": 1, "project": baseline["project"],
              "baseline_sha256": baseline["baseline_sha256"], "revit_build": baseline["revit_build"], "rows": rows}
    return dict(result, preview_sha256=manifest_digest(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--export", action="append", required=True)
    args = parser.parse_args()
    try:
        result = build_preview(load_json(args.baseline), [load_json(path) for path in args.export])
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Project reload preview blocked. {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    return 0


try:
    active_project, query_inputs, current_ui = doc, inputs, uiapp
except NameError:
    if __name__ == "__main__":
        sys.exit(main())
else:
    from pyrevit.api import DB
    try:
        baseline = read_baseline(current_ui, DB, active_project, query_inputs)
        result = {"status": "ok", "snapshot_json": json.dumps(baseline, ensure_ascii=True, allow_nan=False),
                  "audit_token": uuid4().hex, "trial_transaction": query_inputs.get("trial_transaction")}
    except WorkerUnresolved as error:
        result = {"status": "worker_unresolved", "reason": str(error)}
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        result = {"status": "blocked", "reason": str(error)}
