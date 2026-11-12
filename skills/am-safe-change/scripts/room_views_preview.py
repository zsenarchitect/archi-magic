"""Build a review-only room-view preview from a saved-project snapshot (Python 3).

The snapshot contains document_identity, a complete view_names inventory, and
selected rooms. Each room binds its ID/UID, number, level, phase, design option,
host plan, versioned per-family types and templates, ordered finish sources,
footprint, bottom/top elevations, and explicit view requests. An elevation
request has a zero-based side index. A section request has a cut and look.
The native reader must establish the snapshot evidence before any model write.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import room_view_geometry as geometry  # noqa: E402


MAX_BYTES = 2 * 1024 * 1024
MAX_ROOMS = 20
MAX_ROOM_VIEWS = 12
MAX_VIEWS = 80
MAX_NAME_VISITS = 20_000
MAX_GEOMETRY_VISITS = 2_000
MAX_PREPARATION_SECONDS = 30
ELEMENT_ID = re.compile(r"[1-9][0-9]{0,18}")
SHA256 = re.compile(r"[a-f0-9]{64}")
UNSAFE_NAME = re.compile(r'[\x00-\x1f\x7f\\:{}\[\]|;<>?*`~"]')


def encode(data):
    return json.dumps(data, ensure_ascii=True, allow_nan=False, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


def text(value):
    return isinstance(value, str) and bool(value.strip())


def identity(data, versioned=False):
    if (not isinstance(data, dict) or not isinstance(data.get("id"), str)
            or not ELEMENT_ID.fullmatch(data["id"]) or int(data["id"]) > 2**63 - 1
            or not text(data.get("uid")) or (versioned and not text(data.get("version")))):
        raise ValueError("Expected a native element ID and UID with the required version.")
    fields = ("id", "uid", "version") if versioned else ("id", "uid")
    return {field: data[field] for field in fields}


def bounded_list(value, limit, label, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= limit:
        raise ValueError("%s must contain %d to %d entries." % (label, minimum, limit))
    return value


def room_data(room, pair_limit):
    identity(room)
    if not text(room.get("number")) or UNSAFE_NAME.search(room["number"]):
        raise ValueError("Use a nonempty room number with supported view-name characters.")
    identity(room.get("level"))
    identity(room.get("phase"))
    if room.get("design_option") is not None:
        identity(room["design_option"])
    identity(room.get("host_plan"), versioned=True)
    if room.get("geometry_status") != "prismatic_finish_verified":
        raise ValueError("Room evidence must identify a supported prismatic finish envelope.")
    vertices, normals, pairs = geometry.footprint(room.get("footprint"), pair_limit)
    sources = bounded_list(room.get("finish_sources"), len(vertices), "Finish sources", len(vertices))
    for source in sources:
        identity(source)
    return vertices, normals, pairs


def make_view(room, request, section_number, vertices, normals, padding):
    kind = request.get("kind")
    heights = geometry.point(room.get("heights"), 2)
    if kind == "elevation":
        index = request.get("side")
        dimensions = geometry.elevation(vertices, normals, index, heights, padding)
        name = "%s Interior Elevation %02d" % (room["number"], index + 1)
        source = {"side": index, "finish_source": identity(room["finish_sources"][index])}
    elif kind == "section":
        dimensions = geometry.section(vertices, request.get("cut"), request.get("look"), heights, padding)
        name = "%s Interior Section %02d" % (room["number"], section_number)
        source = {"cut": request["cut"], "look": request["look"]}
    else:
        raise ValueError("Choose an elevation side or an explicit section cut.")
    families = room.get("families")
    family = families.get(kind) if isinstance(families, dict) else None
    if not isinstance(family, dict):
        raise ValueError("Select a view type and template for each requested view family.")
    return dict(dimensions, name=name, kind=kind, source=source,
                room_id=room["id"], room_uid=room["uid"],
                type=identity(family.get("type"), versioned=True),
                template=identity(family.get("template"), versioned=True))


def same_view(first, second):
    if first["kind"] != second["kind"]:
        return False
    if first["kind"] == "elevation":
        return first["source"]["side"] == second["source"]["side"]
    return all(
        abs(a - b) <= geometry.TOLERANCE
        for key in ("origin", "look", "crop_min", "crop_max")
        for a, b in zip(first[key], second[key])
    ) and abs(first["far_extent"] - second["far_extent"]) <= geometry.TOLERANCE


def compute(snapshot):
    start = time.monotonic()
    encoded = encode(snapshot)
    if len(encoded) > MAX_BYTES:
        raise ValueError("Snapshot exceeds the byte limit.")
    if not isinstance(snapshot, dict):
        raise ValueError("Expected a saved-project snapshot object.")
    document = snapshot.get("document_identity")
    if (not isinstance(document, dict) or not text(document.get("project_uid"))
            or not isinstance(document.get("path_sha256"), str)
            or not SHA256.fullmatch(document["path_sha256"])):
        raise ValueError("Expected the saved document identity.")
    if snapshot.get("view_names_complete") is not True:
        raise ValueError("A complete native view-name inventory is required.")
    names = bounded_list(snapshot.get("view_names"), MAX_NAME_VISITS, "View names")
    if not all(text(name) for name in names):
        raise ValueError("Every inventoried view needs its native name.")
    used_names = {name.casefold() for name in names}
    rooms = bounded_list(snapshot.get("rooms"), MAX_ROOMS, "Selected rooms", 1)
    padding, = geometry.point([snapshot.get("padding_feet")], 1)
    if padding < 0:
        raise ValueError("Crop padding must be nonnegative.")

    views = []
    warnings = []
    room_ids = set()
    room_uids = set()
    pairs_left = geometry.MAX_INTERSECTION_PAIRS
    geometry_visits = 0
    requested_views = 0
    for room in rooms:
        if time.monotonic() - start > MAX_PREPARATION_SECONDS:
            raise ValueError("Room-view preview exceeded the preparation time limit.")
        identity(room)
        room_id = room["id"]
        if room_id in room_ids or room["uid"] in room_uids:
            raise ValueError("Selected rooms must have unique native IDs and UIDs.")
        room_ids.add(room_id)
        room_uids.add(room["uid"])
        requests = bounded_list(room.get("requests"), MAX_ROOM_VIEWS, "Room views", 1)
        requested_views += len(requests)
        if requested_views > MAX_VIEWS:
            raise ValueError("The selection exceeds the total room-view limit.")
        try:
            vertices, normals, validation_pairs = room_data(room, pairs_left)
        except ValueError as exc:
            warnings.append({"blocking": True, "text": "Room %s: %s" % (room_id, exc)})
            continue
        pairs_left -= validation_pairs
        geometry_visits += 2 * len(vertices)
        if geometry_visits > MAX_GEOMETRY_VISITS:
            raise ValueError("Room geometry exceeds the total visit limit.")
        comparison_cost = 2 * len(vertices) * len(requests) + len(requests) ** 2
        if comparison_cost > pairs_left:
            raise ValueError("Room views exceed the total intersection-pair limit.")
        pairs_left -= comparison_cost

        section_number = 0
        room_views = []
        for request in requests:
            if not isinstance(request, dict):
                raise ValueError("Each view request must be an object.")
            try:
                view = make_view(room, request, section_number + 1, vertices, normals, padding)
                if any(same_view(view, prior) for prior in room_views):
                    continue
                room_views.append(view)
                if view["kind"] == "section":
                    section_number += 1
                name_key = view["name"].casefold()
                if name_key in used_names:
                    raise ValueError("A view named %s already exists or is proposed twice." % view["name"])
            except ValueError as exc:
                warnings.append({"blocking": True, "text": "Room %s: %s" % (room_id, exc)})
                continue
            used_names.add(name_key)
            views.append(view)

    if time.monotonic() - start > MAX_PREPARATION_SECONDS:
        raise ValueError("Room-view preview exceeded the preparation time limit.")
    rows = []
    for view in views:
        width, height = (upper - lower for lower, upper in zip(view["crop_min"], view["crop_max"]))
        look = ", ".join("%.3f" % axis for axis in view["look"][:2])
        rows.append([view["name"], view["room_id"], view["kind"], view["template"]["id"],
                     look, "%.3f x %.3f" % (width, height), "%.3f" % view["far_extent"]])
    proposal = {"snapshot_sha256": hashlib.sha256(encoded).hexdigest(), "views": views}
    preview = {
        "kind": "change-preview",
        "title": "Room interior views",
        "keyColumn": "View",
        "columns": ["View", "Room ID", "Kind", "Template ID", "Look (X, Y)", "Crop (ft)", "Far extent (ft)"],
        "before": [],
        "after": rows,
        "notes": ["Review only. Native containment, view content, and crop checks are still required."],
        "warnings": warnings,
        "blocked": bool(warnings),
        "review_only": True,
        "steps": [],
        "proposal": proposal,
        "proposal_sha256": hashlib.sha256(encode(proposal)).hexdigest(),
    }
    if len(encode(preview)) + 1 > MAX_BYTES:
        raise ValueError("Room-view preview exceeds the evidence byte limit.")
    return preview


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.snapshot.resolve() == args.out.resolve():
            raise ValueError("Choose a different output path from the source snapshot.")
        with args.snapshot.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Snapshot exceeds the byte limit.")
        preview = compute(json.loads(raw.decode("utf-8-sig")))
        with args.out.open("wb") as output:
            output.write(encode(preview))
            output.write(b"\n")
    except (OSError, ValueError, RecursionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 1 if preview["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
