"""Build a review-only placement preview from an exact library selection and live project read."""

import argparse
import json
import math
import sys

from family_library_discovery import manifest_digest, pinned_root, root_path
from family_library_index import select_for_placement
from read_family_library import read_request_file


def finite(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Use finite numbers for model coordinates, level elevation, and rotation.")
    return float(value)


def project_snapshot(value):
    snapshot = value.get("result", value)
    if "snapshot_json" in snapshot:
        snapshot = json.loads(snapshot["snapshot_json"])
    if snapshot.get("kind") != "family-placement-project-snapshot" or snapshot.get("version") != 1:
        raise ValueError("Read the saved project and exact type in Revit first.")
    return snapshot


def parameter_text(record):
    if record["state"] != "value":
        return record["state"].replace("_", " ")
    value = json.dumps(record["value"], ensure_ascii=False)
    return value if len(value) <= 120 else value[:117] + "..."


def build_preview(selection, read, point, rotation):
    selection_basis = {name: value for name, value in selection.items() if name != "selection_sha256"}
    if (selection.get("kind") != "family-library-placement-selection" or selection.get("version") != 1
            or selection.get("selection_sha256") != manifest_digest(selection_basis)):
        raise ValueError("Select one intact family type from the published index.")
    snapshot = project_snapshot(read)
    if (snapshot["selection_sha256"] != selection["selection_sha256"]
            or snapshot["revit_build"] != selection["revit_build"]):
        raise ValueError("The project read does not match this selection and Revit build.")
    if len(point) != 3:
        raise ValueError("Supply one absolute model XYZ point in internal feet.")
    point = [finite(value) for value in point]
    angle = finite(rotation) % 360
    level = snapshot["level"]
    elevation = finite(level["project_elevation"])
    families = snapshot["same_name_families"]
    symbols = snapshot["exact_symbols"]
    symbol = symbols[0] if len(symbols) == 1 else None
    blockers = ["Native placement has not been qualified in the disposable Revit fixture."]
    ownership = []
    differences = []
    if len(symbols) > 1:
        action = "ambiguous"
        blockers.append("More than one project type matches this family, category, and exact type name.")
    elif symbol is not None:
        action = "reuse"
        if not symbol["active"]:
            ownership = [symbol["id"]]
        for field in selection["parameters"]:
            identity = field["id"]
            library_value = selection["type_values"][identity]
            project_value = symbol["parameters"].get(identity, {"state": "unavailable"})
            if project_value != library_value:
                differences.append({"parameter": field["name"], "id": identity,
                                    "library": library_value, "project": project_value})
    else:
        action = "load"
        if families:
            blockers.append("A same-named project family already exists. Preservation during load is unverified.")
        if selection["source"]["catalog"] is not None:
            blockers.append("This family has a companion catalog. Its exact load behavior is unverified.")
    placement_type = symbol["placement_type"] if symbol is not None else selection["placement_type"]
    if placement_type != "OneLevelBased":
        blockers.append("This preview supports only non-hosted point placement on one reviewed level.")

    row = "Family placement"
    coordinates = ", ".join(format(value, ".12g") for value in point)
    parameter_notes = [
        "%s (%s): library %s, project %s." %
        (item["parameter"], item["id"], parameter_text(item["library"]), parameter_text(item["project"]))
        for item in differences
    ]
    activation = {"reuse": "Required" if ownership else "None", "load": "Depends on the loaded symbol",
                  "ambiguous": "Unresolved"}[action]
    dependencies = {"reuse": "No new dependencies", "load": "New family and type only",
                    "ambiguous": "Unresolved"}[action]
    payload = {
        "kind": "family-placement-review", "version": 1, "selection": selection,
        "project": snapshot["project"], "revit_build": snapshot["revit_build"], "level": level,
        "action": action, "symbol": symbol, "same_name_families": families, "host": None,
        "placement_type": placement_type, "absolute_xyz_ft": point, "rotation_deg": angle,
        "level_offset_ft": finite(point[2] - elevation), "ownership_ids": ownership,
    }
    columns = ["Change", "Source", "Family and type", "Type action", "Activation",
               "Level", "Model XYZ (ft)", "Rotation (deg)", "Dependencies"]
    name = selection["family"] + " / " + selection["type"]
    source = selection["source"]["path"]
    preview = {
        "kind": "change-preview", "recipe": "family-library-placement", "title": "Review family placement",
        "keyColumn": "Change", "columns": columns, "changedColumns": columns[3:],
        "before": [[row, source, name, "No placement", "", "", "", "", ""]],
        "after": [[row, source, name, action, activation, level["name"], coordinates, angle, dependencies]],
        "notes": parameter_notes, "warnings": [{"row": row, "text": text, "blocking": True} for text in blockers],
        "blocked": True, "ownership_ids": ownership, "parameter_differences": differences,
        "review": payload, "review_sha256": manifest_digest(payload),
    }
    return preview


def load_json(raw):
    path = root_path(raw)
    with pinned_root(path.parent) as parent:
        return read_request_file(parent, path.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", required=True)
    parser.add_argument("--selection", required=True)
    parser.add_argument("--project-read", required=True)
    parser.add_argument("--xyz", nargs=3, type=float, required=True, metavar=("X", "Y", "Z"))
    parser.add_argument("--rotation", type=float, default=0)
    args = parser.parse_args()
    try:
        selection = load_json(args.selection)
        current = select_for_placement(args.index, selection["generation"], selection["source"]["path"],
                                       selection["type"])
        if current != selection:
            raise ValueError("The published placement selection changed. Read the project again.")
        preview = build_preview(current, load_json(args.project_read), args.xyz, args.rotation)
        print(json.dumps(preview, ensure_ascii=True, allow_nan=False, indent=2))
    except (AttributeError, KeyError, OSError, OverflowError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
