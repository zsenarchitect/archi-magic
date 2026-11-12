"""Preview linked MEP openings and report drift from prepared crossing evidence."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

from mep_penetration_spacing import profile, scalar, spacing_conflicts
from structural_compare_cli import LIMIT, bounded_list, checked_id, digest, identity, read_input, uid

MAX_ELEMENTS = 100
MAX_PAIRS = MAX_ELEMENTS ** 2


def fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def vector(value, size):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError("Invalid measured coordinates.")
    return [scalar(item) for item in value]


def by_uid(rows):
    indexed = {}
    ids = set()
    for row in bounded_list(rows, MAX_ELEMENTS):
        key, identifier = uid(row["uid"]), checked_id(row["id"])
        if key in indexed or identifier in ids:
            raise ValueError("The inventory repeats an element identity.")
        indexed[key] = row
        ids.add(identifier)
    return indexed


def pair_key(row):
    return uid(row["host_uid"]), uid(row["run_uid"])


def measured_geometry(host, run, pair, clearance, tolerance):
    depth = scalar(host["depth_ft"])
    edge_distance = scalar(pair["edge_distance_ft"])
    if host["kind"] not in {"wall", "floor"} or depth <= 0:
        raise ValueError("The host type or depth is unsupported.")
    entry = vector(pair["entry_ft"], 3)
    exit = vector(pair["exit_ft"], 3)
    normal = vector(host["normal"], 3)
    if abs(math.hypot(*normal) - 1.0) > 1e-6:
        raise ValueError("The measured host face normal is not a unit vector.")
    displacement = [b - a for a, b in zip(entry, exit)]
    axial = sum(value * direction for value, direction in zip(displacement, normal))
    lateral = math.hypot(*(value - axial * direction for value, direction in zip(displacement, normal)))
    if not (axial > tolerance and abs(axial - depth) <= tolerance and lateral <= tolerance):
        raise ValueError("The measured pass differs from the perpendicular host depth.")
    section = run["profile"]
    outline = {"shape": section["shape"], "center": vector(pair["center_uv_ft"], 2)}
    if section["shape"] == "round":
        outline["diameter"] = scalar(section["diameter"])
        dimensions = ("diameter",)
    elif section["shape"] == "rectangular":
        outline.update(width=scalar(section["width"]), height=scalar(section["height"]),
                       angle_rad=scalar(pair["angle_rad"]))
        dimensions = ("width", "height")
    else:
        raise ValueError("The measured run profile is unsupported.")
    profile(outline)
    for dimension in dimensions:
        outline[dimension] += 2 * clearance
    _, size = profile(outline)
    radius = size if isinstance(size, float) else math.hypot(*size[:2])
    if edge_distance <= radius + tolerance:
        raise ValueError("The opening clearance cannot be proved inside both trimmed faces.")
    return {"host_sha256": digest(host["gross_sha256"]), "entry_ft": entry, "exit_ft": exit,
            "depth_ft": depth, "clearance_ft": clearance, "outline": outline}


def geometry_matches(prior, current, tolerance):
    first, second = prior["outline"], current["outline"]
    if (first["shape"] != second["shape"] or prior["host_sha256"] != current["host_sha256"]
            or abs(scalar(prior["clearance_ft"]) - current["clearance_ft"]) > tolerance
            or abs(scalar(prior["depth_ft"]) - current["depth_ft"]) > tolerance
            or any(math.dist(vector(prior[key], 3), current[key]) > tolerance for key in ("entry_ft", "exit_ft"))
            or math.dist(vector(first["center"], 2), second["center"]) > tolerance):
        return False
    profile(first)
    if first["shape"] == "round":
        return abs(scalar(first["diameter"]) - second["diameter"]) <= tolerance
    angle = math.remainder(scalar(first["angle_rad"]) - second["angle_rad"], math.pi)
    shift = math.hypot(second["width"], second["height"]) * abs(math.sin(angle / 2))
    return (shift <= tolerance
            and all(abs(scalar(first[key]) - second[key]) <= tolerance for key in ("width", "height")))


def tracked_status(rows, key, host, run, pair, complete, clearance, tolerance):
    if len(rows) != 1:
        return "duplicate", "Several tracked openings claim the same crossing."
    row = rows[0]
    try:
        opening = uid(row["opening_uid"])
        if (digest(row["crossing_key"]) != key or opening != uid(row["observed_uid"])
                or uid(row["observed_host_uid"]) != row["host_uid"] or row["cut_verified"] is not True):
            return "unresolved", "The tracking identity or actual host cut could not be verified."
        if not complete or host is None or run is None or pair is None:
            return "unresolved", "Current link or host evidence is incomplete."
        prior = row["geometry"]
        if fingerprint(prior) != digest(row["geometry_sha256"]):
            return "unresolved", "The saved opening geometry does not match its record."
        if digest(row["opening_sha256"]) != digest(row["observed_opening_sha256"]):
            return "out_of_date", "The opening itself moved or changed size."
        if pair["status"] == "no_crossing" and pair.get("gross_verified") is True:
            return "out_of_date", "The linked run no longer crosses the measured gross host."
        if pair["status"] not in {"solid_crossing", "gross_crossing"} or pair.get("gross_verified") is not True:
            return "unresolved", "The current gross host crossing could not be verified."
        geometry = measured_geometry(host, run, pair, clearance, tolerance)
        if geometry_matches(prior, geometry, tolerance):
            return "current", "The tracked opening and linked crossing still match."
        return "out_of_date", "The linked crossing or host geometry changed."
    except (AttributeError, KeyError, OverflowError, TypeError, ValueError):
        return "unresolved", "The tracked opening has incomplete or unsupported evidence."


def preview(snapshot):
    if snapshot["kind"] != "mep-penetration-evidence" or snapshot["frame"] != "revit_host_internal_feet":
        raise ValueError("Use measured penetration evidence in host internal feet.")
    project = identity(snapshot["host_identity"])
    link = identity(snapshot["link_identity"])
    link_instance = uid(snapshot["link_instance_uid"])
    clearance = scalar(snapshot["clearance_ft"])
    tolerance = scalar(snapshot["tolerance_ft"])
    if clearance < 0 or tolerance <= 0:
        raise ValueError("Use nonnegative clearance and positive measurement tolerance.")
    hosts, runs = by_uid(snapshot["hosts"]), by_uid(snapshot["runs"])
    pairs = {}
    for row in bounded_list(snapshot["pairs"], MAX_PAIRS):
        host_id, run_id = key = pair_key(row)
        if (key in pairs or host_id not in hosts or run_id not in runs
                or row["status"] not in {"solid_crossing", "gross_crossing", "no_crossing", "unresolved"}):
            raise ValueError("A crossing row has a duplicate, unknown identity, or unsupported status.")
        pairs[key] = row
    tracked = {}
    for row in bounded_list(snapshot["tracked"], MAX_PAIRS):
        tracked.setdefault(pair_key(row), []).append(row)

    complete = snapshot["complete"] is True and len(pairs) == len(hosts) * len(runs)
    findings, proposed = {}, {}
    for host_id, run_id in sorted(pairs.keys() | tracked.keys()):
        pair = pairs.get((host_id, run_id))
        host, run = hosts.get(host_id), runs.get(run_id)
        key = fingerprint([project, link, link_instance, host_id, run_id])
        report = {"crossing_key": key, "ruleId": "MEP-PEN-01", "host_uid": host_id, "run_uid": run_id,
                  "host_id": host["id"] if host else None, "linked_run_id": run["id"] if run else None,
                  "system": run.get("system") if run else None}
        existing = tracked.get((host_id, run_id))
        if existing:
            status, reason = tracked_status(existing, key, host, run, pair, complete, clearance, tolerance)
        elif pair["status"] == "no_crossing":
            continue
        elif not complete or host["cuts_complete"] is not True:
            status, reason = "unresolved", "The current scan or nearby-cut inventory is incomplete."
        elif pair["status"] == "gross_crossing" and pair.get("existing_cut_verified") is True:
            status, reason = "untracked", "An existing cut has no verified tracking record."
        elif pair["status"] != "solid_crossing":
            status, reason = "unresolved", "A complete crossing through host material was not verified."
        else:
            try:
                geometry = measured_geometry(host, run, pair, clearance, tolerance)
                location = [a / 2 + b / 2 for a, b in zip(geometry["entry_ft"], geometry["exit_ft"])]
                report.update(geometry=geometry, geometry_sha256=fingerprint(geometry), location=location)
                status, reason = "proposed", "A measured full crossing needs a reviewed opening."
                proposed.setdefault(host_id, []).append(dict(geometry["outline"], key=key))
            except (KeyError, OverflowError, TypeError, ValueError) as error:
                status, reason = "unresolved", str(error)
        findings[key] = dict(report, status=status, reason=reason)

    for host_id, outlines in proposed.items():
        host = hosts[host_id]
        try:
            neighbors = bounded_list(host["existing"], MAX_PAIRS)
            conflicts = spacing_conflicts(outlines, neighbors, host["minimum_web_ft"])
        except (KeyError, OverflowError, TypeError, ValueError) as error:
            for outline in outlines:
                findings[outline["key"]].update(status="unresolved", reason=str(error))
            continue
        proposal_keys = {outline["key"] for outline in outlines}
        for conflict in conflicts:
            for key in (conflict["opening"], conflict["neighbor"]):
                if key in proposal_keys:
                    finding = findings[key]
                    finding.update(status="unresolved", reason="An opening leaves too little material beside another cut.")
                    finding.setdefault("spacing_conflicts", []).append(conflict)
    return {"kind": "mep-penetration-preview", "complete": complete, "findings": list(findings.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    try:
        snapshot, input_sha256 = read_input(args.evidence)
        result = preview(snapshot)
        result["input_sha256"] = input_sha256
        encoded = json.dumps(result, ensure_ascii=True, allow_nan=False)
        if len(encoded) > LIMIT:
            raise ValueError("The penetration preview exceeds 2 MiB. Review a smaller scope.")
        print(encoded)
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(f"Penetration preview blocked. {error}", file=sys.stderr)
        return 2
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
