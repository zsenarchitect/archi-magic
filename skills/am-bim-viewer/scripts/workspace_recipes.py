"""Project catalog projection for explicitly selected saved recipe records."""

import hashlib
import json
import re

from workspace_review_state import unique_object


METADATA_LIMITS = {"name": 200, "description": 2000, "author": 200, "version": 64}


def validate_metadata(metadata):
    if not isinstance(metadata, dict) or set(metadata) != {*METADATA_LIMITS, "slug", "tested_years"}:
        raise ValueError("Use the saved recipe catalog fields.")
    for name, limit in METADATA_LIMITS.items():
        value = metadata[name]
        if not isinstance(value, str) or len(value) > limit or (name != "description" and not value.strip()):
            raise ValueError("The saved recipe has invalid display metadata.")
    slug = metadata["slug"]
    if not isinstance(slug, str) or not 1 <= len(slug) <= 128 or not all(c.isalnum() or c in "-_" for c in slug):
        raise ValueError("The saved recipe has an invalid slug.")
    years = metadata["tested_years"]
    if (not isinstance(years, list) or len(years) > 32
            or any(not isinstance(year, str) or re.fullmatch(r"20[0-9]{2}", year) is None for year in years)
            or len(set(years)) != len(years)):
        raise ValueError("The saved recipe has invalid declared Revit years.")


def make_recipe(payload):
    raw = json.loads(payload, object_pairs_hook=unique_object)
    if (not isinstance(raw, dict) or not isinstance(raw.get("script"), str) or not raw["script"].strip()
            or not isinstance(raw.get("inputs_schema"), dict)):
        raise ValueError("Select a recipe saved by the team recipe library.")
    metadata = {name: raw.get(name) for name in (*METADATA_LIMITS, "slug", "tested_years")}
    years = metadata["tested_years"]
    if isinstance(years, list):
        metadata["tested_years"] = [str(year) if type(year) is int else year for year in years]
    validate_metadata(metadata)
    return {"metadata": metadata, "recipe_sha256": hashlib.sha256(payload).hexdigest()}


def validate_recipe(data):
    if (not isinstance(data, dict) or set(data) != {"metadata", "recipe_sha256"}
            or not isinstance(data["recipe_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", data["recipe_sha256"]) is None):
        raise ValueError("The recipe publication has an invalid source binding.")
    validate_metadata(data["metadata"])
