from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mep_penetration_preview import crossing_key, fingerprint, preview
from mep_penetration_tracking import decode_tracking, opening_measurement, read_tracking, tracking_rows
from read_mep_penetrations import ReadBudget
from structural_compare_cli import identity
from test_mep_penetration_preview import evidence
from test_read_mep_penetrations import XYZ


class Opening(SimpleNamespace):
    pass


def tagged_opening(evidence):
    host = SimpleNamespace(UniqueId="wall-10")
    frame = SimpleNamespace(Origin=XYZ(0, 0, 0), XVector=XYZ(0, 1, 0), YVector=XYZ(0, 0, 1))
    pose = SimpleNamespace(Origin=XYZ(0, 2, 3), BasisX=XYZ(0, 1, 0), BasisY=XYZ(0, 0, 1), BasisZ=XYZ(1, 0, 0))
    opening = Opening(UniqueId="opening-30", Host=host, Symbol=SimpleNamespace(UniqueId="symbol-99"),
                      GetTransform=lambda: pose)
    db = SimpleNamespace(FamilyInstance=Opening, InstanceVoidCutUtils=SimpleNamespace(
        InstanceVoidCutExists=lambda target, item: target is host and item is opening))
    native = SimpleNamespace(GetElement=lambda key: host if key == "wall-10" else None)
    row, = preview(evidence)["findings"]
    cut = dict(row["geometry"]["outline"], key="measured-cut-1")
    evidence["hosts"][0]["existing"] = [cut]
    scope = (identity(evidence["host_identity"]), identity(evidence["link_identity"]), evidence["link_instance_uid"])
    tag = {"kind": "mep-penetration-tracking", "version": 1,
           "host_identity": evidence["host_identity"], "link_identity": evidence["link_identity"],
           "link_instance_uid": evidence["link_instance_uid"], "host_uid": "wall-10", "run_uid": "duct-20",
           "opening_uid": "opening-30", "opening_sha256": opening_measurement(db, host, opening, frame, [cut], 0.001),
           "crossing_key": crossing_key(*scope, "wall-10", "duct-20"),
           "geometry": row["geometry"], "geometry_sha256": row["geometry_sha256"]}
    tag["record_sha256"] = fingerprint(tag)
    return native, db, opening, frame, scope, tag


@pytest.mark.parametrize("change, status", [
    (None, "current"), ("moved_away", "out_of_date"), ("opening_resized", "out_of_date"),
    ("lost_cut", "unresolved"), ("run_not_selected", "unresolved"), ("copied", "duplicate"),
    ("different_link", "untracked"), ("remaining_material", "unresolved"),
])
def test_tracking_inventory_reads_existing_openings_independently(evidence, change, status):
    native, db, opening, frame, scope, tag = tagged_opening(evidence)
    elements = [opening]
    pair = evidence["pairs"][0]
    pair.update(status="gross_crossing", existing_cut_verified=True)
    if change == "moved_away":
        pair["status"] = "no_crossing"
    elif change == "remaining_material":
        pair["status"] = "solid_crossing"
    elif change == "opening_resized":
        evidence["hosts"][0]["existing"][0]["width"] += 0.5
    elif change == "lost_cut":
        db.InstanceVoidCutUtils.InstanceVoidCutExists = lambda *_: False
    elif change == "run_not_selected":
        evidence["runs"] = []
        evidence["pairs"] = []
    elif change == "copied":
        elements.append(Opening(**dict(vars(opening), UniqueId="copied-opening")))
    elif change == "different_link":
        evidence["link_instance_uid"] = "another-link"
        scope = (*scope[:2], "another-link")
    evidence["tracked"], evidence["tracking_issues"] = tracking_rows(
        db, native, elements, lambda _: json.dumps(tag), {"wall-10": (evidence["hosts"][0], frame)},
        *scope, 0.001, ReadBudget())
    assert evidence["tracking_issues"] == []
    row, = preview(evidence)["findings"]
    assert row["status"] == status


def test_corrupt_tag_blocks_a_proposal_even_when_geometry_is_complete(evidence):
    native, db, opening, frame, scope, tag = tagged_opening(evidence)
    damaged = deepcopy(tag)
    damaged["geometry"]["depth_ft"] = 8
    with pytest.raises(ValueError, match="invalid digest"):
        decode_tracking(json.dumps(damaged))
    evidence["tracked"], evidence["tracking_issues"] = tracking_rows(
        db, native, [opening], lambda _: json.dumps(damaged), {"wall-10": (evidence["hosts"][0], frame)},
        *scope, 0.001, ReadBudget())
    assert evidence["tracked"] == []
    report = preview(evidence)
    assert report["complete"] is False
    assert report["tracking_issues"][0]["opening_uid"] == "opening-30"
    assert report["findings"][0]["status"] == "unresolved"


def test_native_schema_query_uses_the_string_field(evidence, monkeypatch):
    native, db, opening, frame, scope, tag = tagged_opening(evidence)
    import mep_penetration_tracking as tracking

    monkeypatch.setitem(sys.modules, "System", SimpleNamespace(Guid=lambda value: value, String=str))
    field = object()

    class GenericGetter:
        def __getitem__(self, value_type):
            assert value_type is str
            return self.get

        def get(self, supplied):
            assert supplied is field
            return json.dumps(tag)

    schema = SimpleNamespace(SchemaName=tracking.SCHEMA_NAME, GetField=lambda name: field if name == tracking.FIELD else None)
    opening.GetEntity = lambda saved: SimpleNamespace(IsValid=lambda: saved is schema, Get=GenericGetter())
    db.ExtensibleStorage = SimpleNamespace(
        ExtensibleStorageFilter=lambda guid: ("filter", guid),
        Schema=SimpleNamespace(Lookup=lambda guid: schema if guid == tracking.SCHEMA_GUID else None))

    def where_passes(item):
        assert item == ("filter", tracking.SCHEMA_GUID)
        return [opening]

    db.FilteredElementCollector = lambda doc: SimpleNamespace(WherePasses=where_passes)
    rows, issues = read_tracking(db, native, {"wall-10": (evidence["hosts"][0], frame)}, *scope, 0.001, ReadBudget())
    assert issues == []
    assert rows[0]["cut_verified"]
    assert rows[0]["opening_sha256"] == rows[0]["observed_opening_sha256"]
