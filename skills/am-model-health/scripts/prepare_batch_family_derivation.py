"""Prepare one writable family copy for a reviewed batch type derivation."""

import argparse
from contextlib import contextmanager
import math
import os
import sys
from time import monotonic
from uuid import uuid4

from batch_family_type_preview import build_preview, validate_record
from family_library_discovery import (
    MAX_FAMILY_BYTES, MAX_SECONDS, MAX_TOTAL_BYTES, checked_file, child_info, directory_info, discover,
    identity, manifest_digest, pinned_directory, pinned_root, root_path,
)
from family_placement_preview import load_json
from read_family_library import encode_record, staged_pair
from stage_family_library import HEADROOM, check_file_record, free_bytes, make_directory, new_file, parts, verify_pair


def ready_rows(lookup, preview, source_path):
    rows = []
    for row, outcome in zip(lookup["rows"], preview["review"]["outcomes"]):
        if row["source_path"] != source_path or outcome["status"] != "ready_for_derivation":
            continue
        sizes = outcome["sizes"]
        if (outcome["line"] != row["line"] or len(sizes) != len(row["sizes"])
                or any({key: value for key, value in actual.items() if key != "internal_value"} != expected
                       or type(actual.get("internal_value")) not in (int, float)
                       or not math.isfinite(actual["internal_value"])
                       for actual, expected in zip(sizes, row["sizes"]))):
            raise ValueError("The project read did not return the complete reviewed size conversions.")
        rows.append({"line": row["line"], "family": row["family"], "category": row["category"],
                     "seed_type": row["seed_type"], "type": row["type"], "sizes": sizes})
    return rows


def create_working_copy(lookup, project_read, preview, staging, source_path, raw_destination):
    """Copy the exact staged RFA without its sidecar, then verify the writable copy."""
    parts(source_path)
    if os.name == "nt" and sys.version_info < (3, 13):
        raise ValueError("Use Python 3.13 or newer for a private Windows working copy.")
    rebuilt = build_preview(lookup, project_read)
    if rebuilt != preview or preview["blocked"]:
        raise ValueError("Use the matching unblocked preview, or review a new eligible subset.")
    request = lookup["request"]
    validate_record(request, "batch-family-type-request", "request_sha256")
    matches = [row for row in request["sources"] if row["path"] == source_path]
    if len(matches) != 1:
        raise ValueError("Select one exact source from the reviewed family type batch.")
    source = matches[0]
    rows = ready_rows(lookup, preview, source_path)
    if not rows:
        return {"status": "no_derivation_needed", "source_path": source_path,
                "review_sha256": preview["review_sha256"]}

    validate_record(staging, "family-library-staging", "staging_sha256")
    if staging["discovery_sha256"] != request["discovery_sha256"]:
        raise ValueError("The staging receipt does not belong to the reviewed discovery.")
    library = root_path(request["root"])
    staged_root = root_path(staging["workspace"])
    destination = root_path(raw_destination)
    if destination.is_relative_to(library) or destination.is_relative_to(staged_root):
        raise ValueError("Put the working copy outside the library and protected staging.")
    live = discover(str(library))
    if live["root_identity"] != request["root_identity"] or [row for row in live["files"]
                                                             if row["path"] == source_path] != [source]:
        raise ValueError("The reviewed source, catalog, or ancestry changed before derivation.")
    staged_bytes = sum(pair["rfa"]["bytes"] + (pair["catalog"]["bytes"] if pair["catalog"] else 0)
                       for pair in staging["pairs"])
    name = "family-derivation-" + uuid4().hex
    working = destination / name
    created = False
    try:
        with staged_pair(staging, source_path) as (path, pair):
            if (pair["rfa"], pair["catalog"]) != (source["rfa"], source["catalog"]):
                raise ValueError("The exact staged files differ from the reviewed source pair.")
            rfa = pair["rfa"]
            reserve = rfa["bytes"] + MAX_FAMILY_BYTES
            if staged_bytes + reserve > MAX_TOTAL_BYTES:
                raise ValueError("The working copy and reserved derived file exceed the batch byte budget.")
            ancestry = []
            with pinned_root(destination, ancestry=ancestry) as parent, pinned_root(path.parent) as pinned_source:
                if request["root_identity"] in ancestry or staging["workspace_identity"] in ancestry:
                    raise ValueError("The working destination resolves under protected input files.")
                if identity(directory_info(pinned_source)) != pair["directory_identity"]:
                    raise ValueError("The protected source directory changed before copying.")
                if free_bytes(parent) - reserve < HEADROOM:
                    raise ValueError("The working and derived files would leave less than 1 GiB free.")
                make_directory(parent, name)
                created = True
                with pinned_directory(parent, name) as scratch:
                    leaf = rfa["basename"]
                    with new_file(scratch, leaf) as stream:
                        copied = checked_file(pinned_source, leaf, child_info(pinned_source, leaf), MAX_FAMILY_BYTES,
                                              {"bytes": 0, "deadline": monotonic() + MAX_SECONDS}, stream)
                    if copied != rfa:
                        raise ValueError("The working copy differs from the staged RFA.")
                    verify_pair(scratch, rfa, None, {"entries": 0, "bytes": 0, "deadline": monotonic() + MAX_SECONDS})
                    scratch_identity = identity(directory_info(scratch))
    except (OSError, ValueError) as error:
        if created:
            raise ValueError(f"{error} The unpublished working directory remains at {working}.") from error
        raise
    record = {
        "kind": "batch-family-type-derivation", "version": 1,
        "request_sha256": request["request_sha256"], "lookup_sha256": lookup["lookup_sha256"],
        "preview_sha256": manifest_digest(preview), "review_sha256": preview["review_sha256"],
        "staging_sha256": staging["staging_sha256"], "staging_workspace_identity": staging["workspace_identity"],
        "source": source, "rows": rows,
        "root": request["root"], "root_identity": request["root_identity"],
        "project": preview["review"]["project"], "revit_build": preview["review"]["revit_build"],
        "workspace": str(working), "workspace_identity": scratch_identity, "working_file": rfa,
    }
    return dict(record, derivation_sha256=manifest_digest(record))


@contextmanager
def verified_working_copy(record):
    """Check the exact writable copy and companion absence before a native family run."""
    validate_record(record, "batch-family-type-derivation", "derivation_sha256")
    root = root_path(record["workspace"])
    basename = parts(record["source"]["path"])[-1]
    rfa = record["working_file"]
    check_file_record(rfa, basename, MAX_FAMILY_BYTES)
    if rfa != record["source"]["rfa"]:
        raise ValueError("The working file no longer binds the reviewed source bytes.")
    with pinned_root(root) as directory:
        if identity(directory_info(directory)) != record["workspace_identity"]:
            raise ValueError("The writable family workspace changed.")
        budget = {"entries": 0, "bytes": 0, "deadline": monotonic() + MAX_SECONDS}
        verify_pair(directory, rfa, None, budget)
        yield root / basename
        verify_pair(directory, rfa, None, budget)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("lookup", "project-read", "preview", "staging", "source", "destination"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        result = create_working_copy(load_json(args.lookup), load_json(args.project_read), load_json(args.preview),
                                     load_json(args.staging), args.source, args.destination)
        print(encode_record(result).decode("ascii"))
    except (AttributeError, KeyError, OSError, OverflowError, RecursionError, TypeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
