from copy import deepcopy
import json
import math
import re
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

from test_room_views_preview import snapshot


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_room_views.py"


class Hook:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, event):
        for handler in self.handlers:
            handler(None, event)


class FixtureDocument:
    IsFamilyDocument = False
    IsWorkshared = False
    IsReadOnly = False
    IsModifiable = False

    def __init__(self):
        self.Application = NS(DocumentChanged=Hook())
        self.elements = {}
        self.log = []
        self.commit_effects = [10, 11, 12]
        self.rollback_effects = [10, 11, 12]
        self.rollback_event = True
        self.committed = False
        self.pending = False

    def Equals(self, other):
        return self is other

    def emit(self, name, operation, added=(), deleted=()):
        self.Application.DocumentChanged.emit(NS(
            GetDocument=lambda: self, GetTransactionNames=lambda: [name], Operation=operation,
            GetAddedElementIds=lambda: [NS(Value=value) for value in added],
            GetModifiedElementIds=lambda: [], GetDeletedElementIds=lambda: [NS(Value=value) for value in deleted],
        ))


class Transaction:
    def __init__(self, document, name):
        self.document, self.name = document, name
        self.state = "uninitialized"

    def GetStatus(self):
        return self.state

    def Start(self):
        self.state = "started"
        return self.state

    def Commit(self):
        self.document.log.append("inner commit")
        if self.document.pending:
            self.state = "pending"
            return self.state
        self.state = "committed"
        self.document.committed = True
        self.document.emit(self.name, "TransactionCommitted", added=self.document.commit_effects)
        return self.state

    def RollBack(self):
        self.state = "rolledback"
        self.document.log.append("inner rollback")
        return self.state


class Group(Transaction):
    def RollBack(self):
        self.state = "rolledback"
        self.document.log.append("group rollback")
        for number in range(10, 14):
            self.document.elements.pop(str(number), None)
        if self.document.rollback_event:
            deleted = self.document.rollback_effects if self.document.committed else []
            self.document.emit(self.name, "TransactionGroupRolledBack", deleted=deleted)
        return self.state


@pytest.fixture
def fixture(monkeypatch):
    document = FixtureDocument()

    def element(number):
        entity = NS(Id=NS(Value=number), UniqueId="fixture-%s" % number)
        document.elements[str(number)] = entity
        return {"id": str(number), "uid": entity.UniqueId}

    source = dict(element(1), host_plan=element(2), level=element(3), phase=element(4),
                  finish_sources=[element(5)], families={"elevation": {"template": element(6)}})
    unrelated = element(7)
    snapshot = {"rooms": [source]}
    views = [
        {"kind": "elevation", "room_id": "1", "template": source["families"]["elevation"]["template"]},
        {"kind": "section", "room_id": "1", "template": source["families"]["elevation"]["template"]},
    ]
    digest = "fixture-digest"
    args = {
        "mode": "rollback_trial", "disposable_fixture": True, "approved_proposal_sha256": digest,
        "room_view_preflight": {"proposal_sha256": digest, "snapshot_json": json.dumps(snapshot),
                                "proposal_json": json.dumps({"views": views})},
        "room_views": {}, "unrelated_control": unrelated,
        "fixture_settings": {"scale": 50, "far_clip_mode": 1},
        "baseline_audit": {
            "audit_token": "b" * 32, "snapshot_sha256": "snapshot", "inventory_sha256": "inventory",
        },
    }

    db = NS(Transaction=Transaction, TransactionGroup=Group, TransactionStatus=NS(
        Started="started", Committed="committed", RolledBack="rolledback", Uninitialized="uninitialized",
    ))
    reader = NS(
        MAX_BYTES=2 * 1024 * 1024,
        native_element=lambda doc, identifier: doc.elements.get(identifier),
        native_preflight=lambda *_: {"proposal_sha256": digest},
        element_identity=lambda entity, _: {"id": str(entity.Id.Value), "uid": entity.UniqueId},
        read_room_view_snapshot=lambda *_: (snapshot, []),
    )
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))
    monkeypatch.setitem(sys.modules, "read_room_finish_boundaries", reader)
    monkeypatch.setitem(sys.modules, "read_room_view_audit", NS(
        TOKEN=re.compile(r"^[0-9a-f]{32}$"), encode=lambda _: "snapshot",
        digest=lambda value: value, inventory=lambda *_: "inventory",
    ))
    run = runpy.run_path(str(SCRIPT))
    namespace = run["trial"].__globals__

    def create(_, spec, _host, _room, _scale, additions):
        if spec["kind"] == "elevation":
            view, marker = element(10), element(11)
            additions.update((view["id"], marker["id"]))
            return view, marker, 0
        view = element(12)
        additions.add(view["id"])
        return view, None, None

    namespace.update(create_view=create, configure_view=lambda *_: None,
                     verify_view=lambda view, marker, *_: {"view": view, "marker": marker})
    return NS(run=lambda: run["trial"](document, args), document=document, namespace=namespace,
              angle=run["horizontal_angle"], args=args)


def test_trial_verifies_added_ids_and_rolls_back(fixture):
    result = fixture.run()
    assert result["status"] == "trial_rolled_back", result
    receipt = json.loads(result["trial_json"])
    assert receipt["effects"] == [
        {"operation": "TransactionCommitted", "added": ["10", "11", "12"], "modified": [], "deleted": []},
        {"operation": "TransactionGroupRolledBack", "added": [], "modified": [], "deleted": ["10", "11", "12"]},
    ]
    assert fixture.document.log == ["inner commit", "group rollback"]
    assert set(fixture.document.elements) == set("1234567")
    assert fixture.document.Application.DocumentChanged.handlers == []


@pytest.mark.parametrize("change", [
    lambda document: document.commit_effects.append(13),
    lambda document: document.rollback_effects.remove(11),
    lambda document: setattr(document, "rollback_event", False),
    lambda document: setattr(document, "pending", True),
])
def test_trial_blocks_on_unresolved_native_transaction(fixture, change):
    change(fixture.document)
    result = fixture.run()
    assert result["status"] == "transaction_unresolved", result
    assert "trial_json" not in result
    assert fixture.document.Application.DocumentChanged.handlers == []


def test_trial_rolls_back_if_view_configuration_fails(fixture):
    def blocked(*_):
        raise ValueError("fixture crop conflict")

    fixture.namespace["configure_view"] = blocked
    result = fixture.run()
    assert result["status"] == "stopped_and_rolled_back", result
    assert result["reason"] == "fixture crop conflict"
    assert fixture.document.log == ["inner rollback", "group rollback"]
    assert "10" not in fixture.document.elements


def test_trial_requires_disposable_fixture_and_reviewed_digest(fixture):
    fixture.document.IsWorkshared = True
    with pytest.raises(ValueError, match="non-workshared"):
        fixture.run()
    fixture.document.IsWorkshared = False
    fixture.args["approved_proposal_sha256"] = "stale"
    with pytest.raises(ValueError, match="exact room view proposal"):
        fixture.run()
    assert fixture.document.log == []


def test_fresh_native_absence_checks_both_id_and_uid(fixture):
    absence = runpy.run_path(str(SCRIPT.with_name("read_room_view_audit.py")))["absence"]
    identity = {"id": "45", "uid": "fixture-added-uid"}
    fixture.document.GetElement = lambda _: None
    assert absence(fixture.document, [identity]) == [identity]
    fixture.document.GetElement = lambda uid: object() if uid == identity["uid"] else None
    with pytest.raises(ValueError, match="still resolves"):
        absence(fixture.document, [identity])
    fixture.document.GetElement = lambda _: None
    fixture.document.elements["45"] = object()
    with pytest.raises(ValueError, match="still resolves"):
        absence(fixture.document, [identity])


def test_elevation_rotation_is_signed(fixture):
    east, north = NS(X=1, Y=0, Z=0), NS(X=0, Y=1, Z=0)
    assert fixture.angle(east, north) == pytest.approx(math.pi / 2)
    assert fixture.angle(north, east) == pytest.approx(-math.pi / 2)


def test_section_box_uses_right_handed_basis_and_positive_depth(fixture):
    class Vector(tuple):
        def __new__(cls, *values):
            return tuple.__new__(cls, values)

        def CrossProduct(self, other):
            x, y, z = self
            i, j, k = other
            return Vector(y * k - z * j, z * i - x * k, x * j - y * i)

    Vector.BasisZ = Vector(0, 0, 1)
    origin, look, right = Vector(2, 3, 5), Vector(1, 0, 0), Vector(0, -1, 0)
    database = fixture.namespace["DB"]
    database.XYZ = Vector
    database.Transform = NS(Identity=NS())
    minima, maxima = {}, {}
    database.BoundingBoxXYZ = lambda: NS(set_MinEnabled=minima.__setitem__, set_MaxEnabled=maxima.__setitem__)
    fixture.namespace["reader"].view_frame = lambda _: (origin, look, right)
    box = fixture.namespace["section_box"]({
        "crop_min": [-2, -4], "crop_max": [3, 4], "far_extent": 8,
    })
    assert box.Transform.Origin == origin
    assert box.Transform.BasisX.CrossProduct(box.Transform.BasisY) == box.Transform.BasisZ == look
    assert box.Min == (-3, -4, 0)
    assert box.Max == (2, 4, 8)
    assert box.Enabled and minima == maxima == {0: True, 1: True, 2: True}


@pytest.fixture
def audit_records(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    import verify_room_view_trial as verifier

    source = snapshot()
    preview = verifier.planner.compute(source)
    name = "Archi Magic room view trial " + "a" * 32
    settings = {"scale": 50, "far_clip_mode": 1}
    baseline = {
        "status": "ok", "audit_token": "b" * 32, "trial_transaction": None, "confirmed_absent": [],
        "snapshot_json": json.dumps(source, sort_keys=True),
        "inventory_json": json.dumps({
            "views": [{"id": "22", "uid": "fixture-existing", "version": "v1", "name": "Existing View"}],
            "control": {"element": {"id": "23", "uid": "fixture-control", "version": "v1"},
                        "parameter_id": "-234", "storage": "String", "value": "measured"},
        }),
    }
    views = []
    additions = []
    for index, spec in enumerate(preview["proposal"]["views"]):
        corners = [[x, y] for x in (spec["crop_min"][0], spec["crop_max"][0])
                   for y in (spec["crop_min"][1], spec["crop_max"][1])]
        identity = {"id": str(30 + index), "uid": "fixture-view-%d" % index}
        additions.append(identity)
        row = dict(
            {key: spec[key] for key in ("name", "kind", "room_id", "template", "far_extent")},
            **settings, type_id=spec["type"]["id"], phase=source["rooms"][0]["phase"],
            view=identity, view_origin=spec["origin"], direction=[-value for value in spec["look"]],
            crop=corners, crop_world=[
                [origin + right * x + up * y for origin, right, up in zip(spec["origin"], spec["right"], (0, 0, 1))]
                for x, y in corners
            ],
        )
        if spec["kind"] == "elevation":
            marker = {"id": str(40 + index), "uid": "fixture-marker-%d" % index}
            row.update(marker=marker, marker_point=spec["origin"], slot=0)
            additions.append(marker)
        views.append(row)
    additions.sort(key=lambda item: int(item["id"]))
    ids = [item["id"] for item in additions]
    receipt = {
        "status": "trial_rolled_back", "transaction": name, "fixture_settings": settings,
        "proposal_sha256": preview["proposal_sha256"], "views": views,
        "baseline_audit": {
            "audit_token": baseline["audit_token"],
            "snapshot_sha256": verifier.digest(baseline["snapshot_json"]),
            "inventory_sha256": verifier.digest(baseline["inventory_json"]),
        },
        "effects": [
            {"operation": "TransactionCommitted", "added": ids, "modified": [], "deleted": []},
            {"operation": "TransactionGroupRolledBack", "added": [], "modified": [], "deleted": ids},
        ],
    }

    def record(mode, run_id, result):
        changes = {key: [] for key in verifier.EFFECTS}
        changes.update({key + "_count": 0 for key in verifier.EFFECTS})
        changes.update(by_category={}, transactions=[] if mode == "query" else [name])
        return {"mode": mode, "status": "ok", "decision": "no_changes", "run_id": run_id, "exit_code": 0,
                "result_truncated": False, "result": result, "changes": changes}

    before = record("query", "before", baseline)
    after = record("query", "after", dict(baseline, audit_token="c" * 32, trial_transaction=name,
                                         confirmed_absent=deepcopy(additions)))
    trial = record("dry_run", "trial", {"status": "trial_rolled_back"})

    def verify():
        trial["result"]["trial_json"] = json.dumps(receipt)
        return verifier.verify(before, trial, after, preview["proposal_sha256"])

    return NS(verify=verify, before=before, trial=trial, after=after, receipt=receipt, additions=additions)


def test_independent_verifier_proves_exact_rollback(audit_records):
    result = audit_records.verify()
    assert result["status"] == "verified_room_view_trial_rollback"
    assert result["confirmed_absent"] == audit_records.additions
    assert result["run_ids"] == ["before", "trial", "after"]


@pytest.mark.parametrize(("change", "reason"), [
    (lambda data: data.after.update(run_id="before"), "three separate runs"),
    (lambda data: data.after["result"].update(inventory_json="{}"), "same reviewed fixture"),
    (lambda data: data.after["result"]["confirmed_absent"][0].update(uid="wrong"), "exact trial additions"),
    (lambda data: data.trial["changes"].update(added_count=1), "empty net effect set"),
    (lambda data: data.receipt["effects"][1]["deleted"].pop(), "native effects"),
    (lambda data: data.receipt["views"][0]["crop_world"][0].__setitem__(2, 999), "displaced"),
])
def test_independent_verifier_rejects_inconsistent_evidence(audit_records, change, reason):
    change(audit_records)
    with pytest.raises(ValueError, match=reason):
        audit_records.verify()
