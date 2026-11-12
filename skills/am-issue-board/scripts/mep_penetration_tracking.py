"""Read the versioned tracking entity attached to a Revit opening instance."""

import json
import math

from mep_penetration_preview import crossing_key, fingerprint, vector
from mep_penetration_spacing import profile, scalar
from structural_compare_cli import checked_id, digest, finite_float, identity, reject_constant, uid, unique_json

SCHEMA_GUID = "be7c896f-91f5-4229-9480-0e469149e7e4"
SCHEMA_NAME = "ArchiMagicMepPenetrationTracking"
FIELD = "RecordJson"
MAX_TRACKED = 1000
MAX_RECORD_BYTES = 8192


def decode_tracking(raw):
    if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("The opening tracking record exceeds 8 KiB.")
    record = json.loads(raw, object_pairs_hook=unique_json, parse_float=finite_float, parse_constant=reject_constant)
    body = {key: value for key, value in record.items() if key != "record_sha256"}
    if (body["kind"] != "mep-penetration-tracking" or type(body["version"]) is not int or body["version"] != 1
            or fingerprint(body) != digest(record["record_sha256"])):
        raise ValueError("The opening tracking record has an unsupported version or invalid digest.")
    host_identity = identity(body["host_identity"])
    link_identity = identity(body["link_identity"])
    link_instance_uid = uid(body["link_instance_uid"])
    host_uid, run_uid = uid(body["host_uid"]), uid(body["run_uid"])
    expected_key = crossing_key(host_identity, link_identity, link_instance_uid, host_uid, run_uid)
    if digest(body["crossing_key"]) != expected_key:
        raise ValueError("The tracking identity does not match its crossing key.")
    uid(body["opening_uid"])
    digest(body["opening_sha256"])
    geometry = body["geometry"]
    if fingerprint(geometry) != digest(body["geometry_sha256"]):
        raise ValueError("The saved geometry does not match its digest.")
    digest(geometry["host_sha256"])
    vector(geometry["entry_ft"], 3)
    vector(geometry["exit_ft"], 3)
    profile(geometry["outline"])
    if scalar(geometry["clearance_ft"]) < 0 or scalar(geometry["depth_ft"]) <= 0:
        raise ValueError("The saved opening geometry has invalid clearance or depth.")
    return body


def opening_measurement(DB, host, opening, frame, existing_cuts, tolerance):
    if (not isinstance(opening, DB.FamilyInstance) or opening.Host is None
            or str(opening.Host.UniqueId) != str(host.UniqueId)
            or not DB.InstanceVoidCutUtils.InstanceVoidCutExists(host, opening)):
        raise ValueError("The exact opening does not cut its recorded host.")
    transform = opening.GetTransform()
    origin = transform.Origin.Subtract(frame.Origin)
    center = [origin.DotProduct(frame.XVector), origin.DotProduct(frame.YVector)]
    nearby = [cut for cut in existing_cuts if math.dist(center, cut["center"]) <= tolerance]
    if len(nearby) != 1:
        raise ValueError("The opening cannot be matched to one measured through-cut.")
    measured = {
        "symbol_uid": uid(str(opening.Symbol.UniqueId)),
        "transform": {key: [scalar(value) for value in (point.X, point.Y, point.Z)]
                      for key in ("Origin", "BasisX", "BasisY", "BasisZ")
                      for point in (getattr(transform, key),)},
        "cut": {key: value for key, value in nearby[0].items() if key != "key"},
    }
    return fingerprint(measured)


def tracking_rows(DB, document, elements, read_entity, hosts, project, link, link_instance, tolerance, budget):
    rows, issues = [], []
    for count, opening in enumerate(elements):
        budget.visit()
        if count >= MAX_TRACKED:
            issues.append({"reason": "The model exceeds the limit of 1,000 tracked openings."})
            break
        try:
            record = decode_tracking(read_entity(opening))
            actual_host = opening.Host if isinstance(opening, DB.FamilyInstance) else None
            actual_host_uid = str(actual_host.UniqueId) if actual_host is not None else None
            host_uid = record["host_uid"]
            if host_uid not in hosts and actual_host_uid not in hosts:
                continue
            if identity(record["host_identity"]) != project:
                raise ValueError("A tracked opening refers to a different saved host model.")
            if record["link_instance_uid"] != link_instance:
                continue
            if identity(record["link_identity"]) != link:
                raise ValueError("A tracked opening refers to a different saved linked model.")
        except Exception as error:
            reason = str(error) if isinstance(error, ValueError) else type(error).__name__
            issues.append({"opening_uid": str(opening.UniqueId), "reason": reason})
            continue

        current = {"host_uid": host_uid, "run_uid": record["run_uid"], "crossing_key": record["crossing_key"],
                   "geometry": record["geometry"], "geometry_sha256": record["geometry_sha256"],
                   "opening_uid": record["opening_uid"], "opening_sha256": record["opening_sha256"],
                   "tracking_sha256": fingerprint(record),
                   "observed_uid": uid(str(opening.UniqueId)), "observed_id": checked_id(str(opening.Id.Value)),
                   "observed_host_uid": actual_host_uid,
                   "cut_verified": False}
        host = hosts.get(host_uid)
        if host is not None and host[1] is not None and current["observed_uid"] == current["opening_uid"]:
            host_row, frame = host
            budget.visit(len(host_row["existing"]))
            try:
                element = document.GetElement(host_uid)
                if element is None or str(element.UniqueId) != host_uid:
                    raise ValueError("The recorded host is unavailable.")
                current["observed_opening_sha256"] = opening_measurement(
                    DB, element, opening, frame, host_row["existing"], tolerance)
                current["cut_verified"] = True
            except Exception as error:
                current["reason"] = str(error) if isinstance(error, ValueError) else type(error).__name__
        rows.append(current)
    return rows, issues


def read_tracking(DB, document, hosts, project, link, link_instance, tolerance, budget):
    from System import Guid, String

    schema_guid = Guid(SCHEMA_GUID)
    storage = DB.ExtensibleStorage
    collector = DB.FilteredElementCollector(document).WherePasses(storage.ExtensibleStorageFilter(schema_guid))
    schema = storage.Schema.Lookup(schema_guid)

    def read_entity(opening):
        if schema is None or schema.SchemaName != SCHEMA_NAME:
            raise ValueError("The opening tracking schema is missing or has a different name.")
        field = schema.GetField(FIELD)
        entity = opening.GetEntity(schema)
        if field is None or not entity.IsValid():
            raise ValueError("The opening has no valid tracking entity.")
        return str(entity.Get[String](field))

    return tracking_rows(DB, document, collector, read_entity, hosts, project, link, link_instance, tolerance, budget)


def write_tracking(DB, opening, record):
    """Attach the verified cut record in the caller's reversible transaction."""
    import clr
    from System import Guid, String

    if not opening.Document.IsModifiable or str(opening.UniqueId) != record["opening_uid"]:
        raise ValueError("The verified opening is outside the active tracking transaction.")
    sealed = dict(record, record_sha256=fingerprint(record))
    encoded = json.dumps(sealed, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    decode_tracking(encoded)

    storage = DB.ExtensibleStorage
    guid = Guid(SCHEMA_GUID)
    schema = storage.Schema.Lookup(guid)
    if schema is None:
        builder = storage.SchemaBuilder(guid)
        try:
            builder.SetSchemaName(SCHEMA_NAME)
            builder.SetReadAccessLevel(storage.AccessLevel.Public)
            builder.SetWriteAccessLevel(storage.AccessLevel.Public)
            builder.AddSimpleField(FIELD, clr.GetClrType(String))
            schema = builder.Finish()
        finally:
            builder.Dispose()
    field = schema.GetField(FIELD)
    if schema.SchemaName != SCHEMA_NAME or field is None or opening.GetEntity(schema).IsValid():
        raise ValueError("The tracking schema conflicts with an existing entity.")
    entity = storage.Entity(schema)
    entity.Set[String](field, encoded)
    opening.SetEntity(entity)
    current = opening.GetEntity(schema)
    if not current.IsValid() or str(current.Get[String](field)) != encoded:
        raise ValueError("Revit did not retain the verified opening tracking record.")
    return sealed["record_sha256"]
