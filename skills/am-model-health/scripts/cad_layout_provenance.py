"""Identify prior Revit placement from the same linked DWG scope."""

import json
import re
from time import monotonic

from Autodesk.Revit.DB.ExtensibleStorage import ContainerType, ExtensibleStorageFilter, Schema
from System import Guid, String
from pyrevit.api import DB

from cad_layout_preview import fields, label
from read_linked_cad_layout import EVIDENCE_LIMIT, encoded, sha256

SCHEMA_GUID = "2d53c41d-c6e0-44c1-b800-1ab7d1364e82"
SCHEMA_NAME = "ArchiMagicCadLayoutV1"
MAX_TAGS = 5000
MAX_TAG_BYTES = 16 * 1024
MAX_TOTAL_TAG_BYTES = 16 * 1024 * 1024
KINDS = {"wall", "door", "room", "separator"}
LAYER_ROLES = {"walls", "doors", "room_layers"}


def digest(value):
    return sha256(encoded(value, EVIDENCE_LIMIT))


def placement_manifest(evidence, preview, snapshot, mapping):
    proposal = preview["proposal"]
    scope = {"project_uid": evidence["document"]["project_uid"], "link_uid": proposal["link_id"],
             "level_uid": proposal["level_id"], "layers": {
                 sha256(name): role for role in LAYER_ROLES for name in mapping[role]
             }}
    lineage = digest(scope)
    source = {row["id"]: row for row in [*snapshot["segments"], *snapshot["room_loops"]]}
    types = {row["type_id"]: row["type_uid"] for row in evidence["type_evidence"]}
    pieces = {}

    def add(kind, source_ids, geometry, choices=None, edge=None):
        key = digest([lineage, kind, [source[token] for token in sorted(source_ids)], edge])
        if key in pieces:
            raise ValueError("The CAD proposal repeats a placement piece.")
        expected = digest([preview["view"]["phase_uid"], proposal["level_mm"], proposal["tolerance_mm"],
                           geometry, choices])
        pieces[key] = {"kind": kind, "expected_sha256": expected, "source_ids": sorted(source_ids), "edge": edge}
        return key

    wall_keys = {}
    for wall in proposal["walls"]:
        wall_keys[wall["id"]] = add("wall", wall["source_ids"],
                                   [wall["axis"], wall["width_mm"], types[wall["type_id"]]],
                                   preview["wall_choices"][wall["id"]])
    for door in proposal["doors"]:
        add("door", door["source_ids"], [wall_keys[door["host"]], door["location"], door["span_mm"],
                                        types[door["type_id"]]], preview["door_choices"][door["source_ids"][0]])
    for room in proposal["rooms"]:
        edges = []
        for boundary in room["boundary_edges"]:
            if boundary["kind"] == "separator":
                edge = sorted(boundary["edge"])
                key = add("separator", room["source_ids"], edge, edge=edge)
                edges.append([key])
            else:
                edges.append([wall_keys[boundary["wall_id"]], boundary["face_sign"]])
        add("room", room["source_ids"], [room["boundary"], room["interior_point"], edges])
    return {"scope": scope, "lineage_sha256": lineage, "plan_sha256": digest(pieces), "pieces": pieces}


def unique_fields(pairs):
    record = dict(pairs)
    if len(record) != len(pairs):
        raise ValueError("duplicate_provenance_field")
    return record


def is_digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def parse_tag(payload):
    tag = json.loads(payload, object_pairs_hook=unique_fields)
    fields(tag, "scope lineage_sha256 plan_sha256 piece_key kind element_uid expected_sha256 observed_sha256")
    scope = fields(tag["scope"], "project_uid link_uid level_uid layers")
    for key in ("project_uid", "link_uid", "level_uid"):
        label(scope[key])
    label(tag["element_uid"])
    layers = scope["layers"]
    if (not isinstance(layers, dict) or not 1 <= len(layers) <= 128
            or any(not is_digest(key) or role not in LAYER_ROLES for key, role in layers.items())
            or tag["kind"] not in KINDS
            or any(not is_digest(tag[key]) for key in (
                "lineage_sha256", "plan_sha256", "piece_key", "expected_sha256", "observed_sha256"))
            or tag["lineage_sha256"] != digest(scope)):
        raise ValueError("invalid_provenance_tag")
    return tag


def payload_field(schema):
    fields_ = list(schema.ListFields())
    if (schema.SchemaName != SCHEMA_NAME or len(fields_) != 1 or fields_[0].FieldName != "Payload"
            or fields_[0].ValueType.FullName != "System.String" or fields_[0].ContainerType != ContainerType.Simple):
        raise ValueError("unexpected_provenance_schema")
    return fields_[0]


def audit_tags(document, manifest, deadline):
    scope = manifest["scope"]
    matches, conflicts = {}, []
    limit = None
    guid = Guid(SCHEMA_GUID)
    schema = Schema.Lookup(guid)
    if schema is not None:
        try:
            field = payload_field(schema)
        except ValueError as error:
            return {"status": "incomplete", "conflicts": [{"reason": str(error)}]}
        collector = DB.FilteredElementCollector(document)
        tag_filter = ExtensibleStorageFilter(guid)
        total_bytes = 0
        try:
            for index, element in enumerate(collector.WherePasses(tag_filter)):
                if index >= MAX_TAGS or monotonic() >= deadline:
                    limit = "tags" if index >= MAX_TAGS else "time"
                    break
                identifier = str(element.Id.Value)
                try:
                    entity = element.GetEntity(schema)
                    payload = entity.Get[String](field) if entity.IsValid() else None
                    if not isinstance(payload, str) or len(payload) > MAX_TAG_BYTES or not payload.isascii():
                        raise ValueError("invalid_provenance_payload")
                    total_bytes += len(payload)
                    if total_bytes > MAX_TOTAL_TAG_BYTES:
                        limit = "bytes"
                        break
                    tag = parse_tag(payload)
                except Exception:
                    conflicts.append({"id": identifier, "reason": "unreadable_provenance"})
                    continue
                previous = tag["scope"]
                if any(previous[key] != scope[key] for key in ("project_uid", "link_uid", "level_uid")):
                    continue
                if not previous["layers"].keys() & scope["layers"].keys():
                    continue
                key = tag["piece_key"]
                planned = manifest["pieces"].get(key)
                if previous != scope:
                    reason = "overlapping_layer_scope"
                elif str(element.UniqueId) != tag["element_uid"]:
                    reason = "copied_provenance"
                elif key in matches:
                    reason = "duplicate_piece_key"
                elif (tag["plan_sha256"] != manifest["plan_sha256"] or planned is None
                      or any(planned[field_name] != tag[field_name] for field_name in ("kind", "expected_sha256"))):
                    reason = "stale_placement"
                else:
                    matches[key] = {"id": identifier, "observed_sha256": tag["observed_sha256"]}
                    continue
                conflicts.append({"id": identifier, "reason": reason})
        finally:
            tag_filter.Dispose()
            collector.Dispose()
        if monotonic() >= deadline:
            limit = "time"
    missing = sorted(manifest["pieces"].keys() - matches.keys()) if matches else []
    status = ("incomplete" if limit else "conflict" if conflicts or missing else
              "matching_tags_pending_readback" if matches else "no_tags")
    return {"status": status, "limit": limit, "matches": matches,
            "conflicts": sorted(conflicts, key=lambda row: int(row["id"])),
            "missing_piece_keys": missing}
