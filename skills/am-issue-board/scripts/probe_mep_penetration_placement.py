"""Review and trial exact linked MEP openings in a disposable project."""

import argparse
import json
import math
import re
from pathlib import Path
import sys
from uuid import uuid4

from mep_penetration_preview import fingerprint, preview
from mep_penetration_spacing import scalar, spacing_conflicts
from mep_penetration_tracking import opening_measurement, read_tracking, write_tracking
from read_mep_penetrations import ReadBudget, area, matches_outline, principal_host, principal_references, project_inventory, read, selected
from structural_compare_cli import checked_id, digest, identity, read_input, uid


def placement_plan(snapshot, choices):
    review = preview(snapshot)
    keys = choices["crossing_keys"]
    if (not review["complete"] or not isinstance(keys, list) or not 1 <= len(keys) <= 10
            or len(keys) != len(set(keys))):
        raise ValueError("Review a complete scan and one to ten distinct crossings.")
    eligible = {row["crossing_key"]: row for row in review["findings"] if row["status"] == "proposed"}
    hosts = {row["uid"]: row for row in snapshot["hosts"]}
    symbols = {row["id"]: row for row in snapshot["symbols"]}
    rows, scope = [], set()
    for key in keys:
        if digest(key) not in eligible:
            raise ValueError("A reviewed crossing is no longer eligible for an opening.")
        finding = eligible[key]
        host = hosts[finding["host_uid"]]
        shape = finding["geometry"]["outline"]["shape"]
        mapping = choices["symbols"][host["kind"] + ":" + shape]
        required = {"diameter", "depth"} if shape == "round" else {"width", "height", "depth"}
        parameters = mapping["parameters"]
        if parameters.keys() != required or len(set(parameters.values())) != len(parameters):
            raise ValueError("Bind each size to one exact instance parameter.")
        symbol_id, symbol_uid = checked_id(mapping["id"]), uid(mapping["uid"])
        measured = symbols.get(symbol_id)
        if (measured is None or measured["uid"] != symbol_uid or measured["placement_type"] != "WorkPlaneBased"
                or not measured["version_guid"] or type(measured["is_active"]) is not bool):
            raise ValueError("The mapped opening symbol lacks exact reviewed native evidence.")
        symbol = dict(measured, parameters={key: checked_id(value) for key, value in parameters.items()})
        scope.update((host["id"], symbol["id"]))
        rows.append({"crossing_key": key, "host_uid": host["uid"], "host_id": host["id"],
                     "run_uid": finding["run_uid"], "symbol": symbol, "geometry": finding["geometry"]})
    return {"kind": "change-preview", "title": "Place openings for linked MEP crossings",
            "keyColumn": "Crossing", "columns": ["Crossing", "Host", "Opening"], "changedColumns": ["Opening"],
            "before": [[row["crossing_key"], row["host_id"], "No opening"] for row in rows],
            "after": [[row["crossing_key"], row["host_id"],
                       json.dumps(row["geometry"]["outline"], sort_keys=True)] for row in rows],
            "ownership_ids": sorted(scope, key=int), "source_sha256": fingerprint(snapshot), "rows": rows}


def one_element(document, DB, identifier, unique_id):
    element, = selected(document, DB, [identifier])
    if str(element.UniqueId) != unique_id:
        raise ValueError("An element no longer has its reviewed identity.")
    return element


def set_sizes(DB, opening, binding, geometry, tolerance):
    from System import Double

    values = dict(geometry["outline"], depth=geometry["depth_ft"])
    parameters = {}
    for parameter in opening.Parameters:
        key = str(parameter.Id.Value)
        if key in binding.values():
            if key in parameters:
                raise ValueError("A size parameter identity is ambiguous.")
            parameters[key] = parameter
    if parameters.keys() != set(binding.values()):
        raise ValueError("An approved size parameter is missing on the instance.")
    for role, identifier in binding.items():
        parameter = parameters[identifier]
        if (parameter.IsReadOnly or parameter.StorageType != DB.StorageType.Double
                or parameter.Definition.GetDataType() != DB.SpecTypeId.Length):
            raise ValueError("An approved size field is not a writable instance length.")
        expected = values[role]
        if abs(scalar(parameter.AsDouble()) - expected) > tolerance and not parameter.Set(Double(expected)):
            raise ValueError("Revit did not set the reviewed opening size.")
        if abs(scalar(parameter.AsDouble()) - expected) > tolerance:
            raise ValueError("The opening did not retain the reviewed size.")


def verify_new_cut(before, after, geometry, tolerance):
    if (before["gross_sha256"] != after["gross_sha256"] or before["uid"] != after["uid"]
            or abs(before["depth_ft"] - after["depth_ft"]) > tolerance):
        raise ValueError("The opening changed the gross host geometry.")
    unmatched = list(after["existing"])
    for old in before["existing"]:
        matches = [index for index, current in enumerate(unmatched) if matches_outline(old, current, tolerance)]
        if len(matches) != 1:
            raise ValueError("A prior opening changed during the cut.")
        unmatched.pop(matches[0])
    if len(unmatched) != 1 or not matches_outline(unmatched[0], geometry["outline"], tolerance):
        raise ValueError("The actual through-cut does not match the reviewed outline.")
    new = dict(unmatched[0], key="new-cut")
    old = [dict(row, key=f"old-cut-{index}") for index, row in enumerate(before["existing"])]
    if spacing_conflicts([new], old, before["minimum_web_ft"]):
        raise ValueError("The new cut leaves too little material beside another opening.")
    return unmatched[0], area(new) * after["depth_ft"]


def place_one(document, DB, snapshot, row, budget):
    tolerance = snapshot["tolerance_ft"]
    host = one_element(document, DB, row["host_id"], row["host_uid"])
    mapping = row["symbol"]
    symbol = one_element(document, DB, mapping["id"], mapping["uid"])
    if (not isinstance(symbol, DB.FamilySymbol)
            or symbol.Family.FamilyPlacementType != DB.FamilyPlacementType.WorkPlaneBased):
        raise ValueError("Use the reviewed face-based opening family symbol.")
    if not DB.InstanceVoidCutUtils.CanBeCutWithVoid(host):
        raise ValueError("The reviewed host does not support the void cut.")
    activated = not symbol.IsActive
    if activated:
        symbol.Activate()
        document.Regenerate()
    minimum_web = next(item["minimum_web_ft"] for item in snapshot["hosts"] if item["uid"] == row["host_uid"])
    before, native = principal_host(DB, host, minimum_web, tolerance, budget)
    geometry = row["geometry"]
    if before["gross_sha256"] != geometry["host_sha256"]:
        raise ValueError("The gross host changed before placement.")
    frame = native[1]
    before_volume = float(native[4].Volume)
    _, refs = principal_references(DB, host)
    angle = geometry["outline"].get("angle_rad", 0)
    direction = frame.XVector.Multiply(math.cos(angle)).Add(frame.YVector.Multiply(math.sin(angle)))
    point = DB.XYZ(*geometry["entry_ft"])
    opening = document.Create.NewFamilyInstance(refs[0], point, direction, symbol)
    if (not isinstance(opening, DB.FamilyInstance) or opening.Host is None
            or str(opening.Host.UniqueId) != row["host_uid"] or str(opening.Symbol.UniqueId) != mapping["uid"]):
        raise ValueError("Revit placed a different opening or host.")
    set_sizes(DB, opening, mapping["parameters"], geometry, tolerance)
    document.Regenerate()
    cut_utils = DB.InstanceVoidCutUtils
    if not cut_utils.IsVoidInstanceCuttingElement(opening):
        raise ValueError("The placed opening has no qualified cutting void.")
    if not cut_utils.InstanceVoidCutExists(host, opening):
        cut_utils.AddInstanceVoidCut(document, host, opening)
    document.Regenerate()

    current, native = principal_host(DB, host, minimum_web, tolerance, budget)
    cut_outline, expected_volume = verify_new_cut(before, current, geometry, tolerance)
    removed_volume = before_volume - float(native[4].Volume)
    if removed_volume <= 0 or abs(removed_volume - expected_volume) > tolerance * expected_volume / geometry["depth_ft"]:
        raise ValueError("The measured material removal differs from the reviewed through-cut.")
    if opening.GetTransform().Origin.DistanceTo(point) > tolerance:
        raise ValueError("The measured opening position differs from the reviewed point.")
    observed = opening_measurement(DB, host, opening, native[1], current["existing"], tolerance)
    tag = {"kind": "mep-penetration-tracking", "version": 1,
           "host_identity": snapshot["host_identity"], "link_identity": snapshot["link_identity"],
           "link_instance_uid": snapshot["link_instance_uid"], "host_uid": row["host_uid"], "run_uid": row["run_uid"],
           "opening_uid": str(opening.UniqueId), "crossing_key": row["crossing_key"], "geometry": geometry,
           "geometry_sha256": fingerprint(geometry), "opening_sha256": observed}
    tag_digest = write_tracking(DB, opening, tag)
    budget.visit()
    return {"crossing_key": row["crossing_key"], "opening_id": str(opening.Id.Value),
            "opening_uid": str(opening.UniqueId), "opening_sha256": observed, "tracking_sha256": tag_digest,
            "removed_volume_ft3": removed_volume, "cut_outline": cut_outline,
            "activated_symbol": mapping["id"] if activated else None}


def verify_after_commit(document, DB, snapshot, plan, rows, budget):
    tolerance = snapshot["tolerance_ft"]
    minimum_web = {item["uid"]: item.get("minimum_web_ft") for item in snapshot["hosts"]}
    hosts = {}
    for item in plan["rows"]:
        if item["host_uid"] not in hosts:
            host = one_element(document, DB, item["host_id"], item["host_uid"])
            current, native = principal_host(DB, host, minimum_web[item["host_uid"]], tolerance, budget)
            if current["gross_sha256"] != item["geometry"]["host_sha256"]:
                raise ValueError("A provisionally kept host changed after regeneration.")
            hosts[item["host_uid"]] = (current, native[1])
    observed, issues = read_tracking(DB, document, hosts, identity(snapshot["host_identity"]),
                                     identity(snapshot["link_identity"]), snapshot["link_instance_uid"],
                                     tolerance, budget)
    if issues:
        raise ValueError("The provisionally placed openings have incomplete tracking evidence.")
    for row in rows:
        matches = [item for item in observed if item["crossing_key"] == row["crossing_key"]]
        if (len(matches) != 1 or matches[0]["observed_uid"] != row["opening_uid"] or not matches[0]["cut_verified"]
                or matches[0]["observed_opening_sha256"] != row["opening_sha256"]
                or matches[0]["tracking_sha256"] != row["tracking_sha256"]):
            raise ValueError("A provisional opening or tracking record changed at commit.")


def verified_effects(plan, rows, original, current, events):
    added = {row["opening_id"] for row in rows}
    hosts = {row["host_id"] for row in plan["rows"]}
    allowed = hosts | {row["activated_symbol"] for row in rows if row["activated_symbol"] is not None}
    if len(events) != 1 or len(added) != len(rows) or set(current) - set(original) != added:
        raise ValueError("Revit did not expose exactly the reviewed new opening instances.")
    event = events[0]
    changed = {key for key in current.keys() & original.keys() if current[key] != original[key]}
    if (set(original) - set(current) or set(event["added"]) != added or event["deleted"]
            or not hosts <= set(event["modified"]) or set(event["modified"]) - allowed - added
            or changed - set(event["modified"])
            or any(current[key][0] != original[key][0] for key in changed)
            or any(current[row["opening_id"]][0] != row["opening_uid"] for row in rows)):
        raise ValueError("The trial changed elements outside its reviewed host and opening scope.")
    return event


TRANSACTION_NAME = "Trial linked MEP openings"


def execute_trial(document, DB, snapshot, plan, reread, *, baseline_token=None, baseline_inventory=None):
    name = TRANSACTION_NAME + " " + uuid4().hex
    status = DB.TransactionStatus
    group = DB.TransactionGroup(document, name)
    transaction = DB.Transaction(document, name)
    budget = ReadBudget(seconds=30)
    original = project_inventory(document, DB, budget)
    inventory_digest = fingerprint(original)
    if baseline_inventory is not None and baseline_inventory != inventory_digest:
        raise ValueError("The project inventory changed after the reviewed baseline.")
    rows, events = [], []
    failure = None
    restored = subscribed = closing = unexpected = unresolved = False
    effects = None

    def record(sender, event):
        nonlocal unexpected
        if closing:
            return
        try:
            if (events or not event.GetDocument().Equals(document) or str(event.Operation) != "TransactionCommitted"
                    or list(event.GetTransactionNames()) != [name]):
                unexpected = True
                return
            events.append({"added": sorted(str(item.Value) for item in event.GetAddedElementIds()),
                           "deleted": sorted(str(item.Value) for item in event.GetDeletedElementIds()),
                           "modified": sorted(str(item.Value) for item in event.GetModifiedElementIds())})
        except Exception:
            unexpected = True

    try:
        document.Application.DocumentChanged += record
        subscribed = True
        if group.Start() != status.Started or transaction.Start() != status.Started:
            raise ValueError("Revit did not start the reversible placement trial.")
        for row in plan["rows"]:
            budget.visit()
            rows.append(place_one(document, DB, snapshot, row, budget))
        if transaction.Commit() != status.Committed or transaction.GetStatus() != status.Committed:
            raise ValueError("The provisional placement transaction did not finish.")
        verify_after_commit(document, DB, snapshot, plan, rows, budget)
        if unexpected:
            raise ValueError("Revit reported an unexpected project transaction.")
        effects = verified_effects(plan, rows, original, project_inventory(document, DB, budget), events)
    except Exception as error:
        unresolved = type(error).__name__ == "RegenerationFailedException"
        failure = str(error) if isinstance(error, ValueError) else type(error).__name__
    finally:
        closing = True
        try:
            current = transaction.GetStatus()
            if current == status.Started:
                if transaction.RollBack() != status.RolledBack:
                    raise RuntimeError("The inner transaction did not roll back.")
                current = transaction.GetStatus()
            if current not in (status.Uninitialized, status.Committed, status.RolledBack):
                raise RuntimeError("The inner transaction remains unresolved.")
            if group.GetStatus() == status.Started:
                restored = group.RollBack() == status.RolledBack and group.GetStatus() == status.RolledBack
            else:
                restored = group.GetStatus() == status.Uninitialized and current == status.Uninitialized
        except Exception:
            restored = False
        finally:
            if subscribed:
                try:
                    document.Application.DocumentChanged -= record
                except Exception:
                    restored = False
            for scope in (transaction, group):
                try:
                    scope.Dispose()
                except Exception:
                    restored = False

    try:
        restored = (restored and not unresolved and reread() == snapshot
                    and project_inventory(document, DB, ReadBudget()) == original
                    and all(document.GetElement(row["opening_uid"]) is None for row in rows))
    except Exception:
        restored = False
    if not restored:
        return {"status": "transaction_unresolved", "reason": failure or "The original scope was not restored."}
    if failure:
        return {"status": "stopped", "reason": failure, "rollback_verified": True}
    return {"status": "trial_rolled_back", "transaction": name, "rows": rows,
            "preview_sha256": fingerprint(plan), "effects": effects, "rollback_verified": True,
            "baseline_audit_token": baseline_token, "baseline_inventory_sha256": inventory_digest}


def trial(document, DB, args):
    if (args["disposable_fixture"] is not True or document.IsWorkshared
            or str(document.Application.VersionNumber) != "2026"):
        raise ValueError("Use a saved standalone disposable Revit 2026 project.")
    baseline_token = args["baseline_audit_token"]
    if not isinstance(baseline_token, str) or not re.fullmatch(r"[0-9a-f]{32}", baseline_token):
        raise ValueError("Use the audit token from the complete original project query.")
    baseline_inventory = digest(args["baseline_inventory_sha256"])
    request = args["read"]

    def reread():
        return json.loads(read(DB, document, request)["snapshot_json"])

    snapshot = reread()
    plan = placement_plan(snapshot, args["choices"])
    if fingerprint(plan) != args["preview_sha256"]:
        raise ValueError("The live crossing evidence or mapping changed after preview.")
    return execute_trial(document, DB, snapshot, plan, reread,
                         baseline_token=baseline_token, baseline_inventory=baseline_inventory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("choices", type=Path)
    args = parser.parse_args()
    try:
        plan = placement_plan(read_input(args.evidence)[0], read_input(args.choices)[0])
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Opening preview blocked. {error}", file=sys.stderr)
        return 2
    print(json.dumps({"preview": plan, "preview_sha256": fingerprint(plan)}, ensure_ascii=True, allow_nan=False))
    return 0


try:
    active_document, query_inputs = doc, inputs
except NameError:
    if __name__ == "__main__":
        sys.exit(main())
else:
    from pyrevit.api import DB
    try:
        result = trial(active_document, DB, query_inputs)
    except Exception as error:
        result = {"status": "stopped", "reason": str(error) if isinstance(error, ValueError) else type(error).__name__}
