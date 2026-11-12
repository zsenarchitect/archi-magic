"""Check that a provisional family reload preserves project overrides and exact definitions."""

from family_library_discovery import manifest_digest
from read_family_library import WorkerUnresolved
from read_family_parameter_transfer import inspect_loaded, project_values
from read_family_placement import eid


def verify_project_values(before, after, additions):
    if before["instances"] != after["instances"]:
        raise ValueError("The reload changed existing instance values or identities.")
    old_types = {row["id"]: row for row in before["types"]}
    new_types = {row["id"]: row for row in after["types"]}
    if (len(old_types) != len(before["types"]) or len(new_types) != len(after["types"])
            or old_types.keys() != new_types.keys()):
        raise ValueError("The reload changed the existing loaded type identities.")
    for identifier, original in old_types.items():
        loaded = new_types[identifier]
        if ({key: value for key, value in original.items() if key != "parameters"}
                != {key: value for key, value in loaded.items() if key != "parameters"}):
            raise ValueError("The reload changed an existing type identity.")
        old_fields, new_fields = original["parameters"], loaded["parameters"]
        if any(new_fields.get(key) != value for key, value in old_fields.items()):
            raise ValueError("The reload changed an existing type value or parameter identity.")
        added_fields = [value for key, value in new_fields.items() if key not in old_fields]
        by_name = {row["name"]: row for row in added_fields}
        if by_name.keys() != additions.keys() or len(by_name) != len(added_fields):
            raise ValueError("The loaded type exposes a different set of new parameters.")
        for name, expected in additions.items():
            row = by_name[name]
            if (row["owner_id"] != identifier or row["owner_uid"] != original["uid"]
                    or row["shared_guid"] is not None or row["built_in"] is not None
                    or row["data_type"] != expected["data_type"] or row["storage"] != expected["storage"]
                    or {key: row[key] for key in ("state", "value") if key in row}
                    != expected["type_values"][original["type_name"]]):
                raise ValueError(f"The loaded type value or definition for {name!r} differs from the sealed family.")


def verify_loaded_family(ui, DB, document, family, original, sealed):
    identity = {"id": eid(family), "uid": str(family.UniqueId)}
    if identity != original["loaded_family"] or identity != sealed["loaded_family"]:
        raise ValueError("Revit did not return the exact reviewed existing project family.")
    values = project_values(document, DB, family)
    read = inspect_loaded(ui, DB, document, family)
    if project_values(document, DB, family) != values:
        raise WorkerUnresolved("Project parameter values changed during the loaded-family inspection.")
    if (any(read[key] != sealed[key] for key in ("family", "category_id", "revit_build", "nested_families"))
            or read["state"]["coverage"] != {"complete": True, "issues": []}
            or manifest_digest(read["state"]) != sealed["state_sha256"]):
        raise ValueError("The provisionally loaded family differs from the independently reopened export.")
    verify_project_values(original["project_values"], values, sealed["additions"])
    return {"loaded_family": identity, "state_sha256": sealed["state_sha256"],
            "project_values_sha256": manifest_digest(values)}
