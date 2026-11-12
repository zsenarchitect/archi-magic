import json
import math
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


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
