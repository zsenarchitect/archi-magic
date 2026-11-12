from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


class RegenerationFailure(Exception):
    pass


class Collection(list):
    Count = property(len)


class Handler:
    def __iadd__(self, callback):
        assert not hasattr(self, "callback")
        self.callback = callback
        return self

    def __isub__(self, callback):
        assert self.callback is callback
        del self.callback
        return self


@pytest.fixture
def native(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    db = NS(TransactionStatus=NS(Started="Started", Committed="Committed", Pending="Pending",
                                 RolledBack="RolledBack", Uninitialized="Uninitialized"))
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.Exceptions",
                        NS(RegenerationFailedException=RegenerationFailure))
    monkeypatch.setitem(sys.modules, "System.Diagnostics", NS(Stopwatch=NS(StartNew=object)))
    monkeypatch.setitem(sys.modules, "pyrevit.api", NS(DB=db))

    def capture(events):
        assert len(events) == 1 and events[0] is not None
        return {field: sorted(ids, key=int) for field, ids in zip(("added", "modified", "deleted"), events[0])}

    monkeypatch.setitem(sys.modules, "duplicate_views", NS(capture_changes=capture))
    module = runpy.run_path(str(SCRIPTS / "probe_mep_route.py"))
    return db, module["run"].__globals__


@pytest.mark.parametrize("fault, expected", [
    (None, "mep_route_trial_rolled_back"), ("netted", "mep_route_trial_rolled_back"),
    ("wrong_review", "stopped"),
    ("added", "stopped_and_rolled_back"), ("pending", "transaction_unresolved"),
    ("regen", "transaction_unresolved"), ("envelope", "stopped_and_rolled_back"),
    ("foreign", "transaction_unresolved"), ("survives", "transaction_unresolved"),
])
def test_route_trial_uses_live_review_and_rolls_back(native, monkeypatch, fault, expected):
    db, module = native
    document = NS(Application=NS(DocumentChanged=Handler()))
    document.Equals = lambda other: document is other
    objects, actions = {}, []
    accepted = {"source": "host"}
    owner_snapshot = {"endpoints": [{"element_id": "10"}, {"element_id": "11"}]}
    document.GetElement = lambda identifier: actions.append(("resolve", identifier)) or objects.get(identifier)
    args = {"preflight": accepted if fault != "wrong_review" else {}, "route_input_json": "{}",
            "preview": {"route": {}}, "owners_record": {"snapshot": owner_snapshot},
            "types_record": {"types": {}}}
    reader = NS(element_id=lambda value: value)

    def preflight(*_):
        actions.append("preflight")
        return accepted

    monkeypatch.setattr(module["preflight"], "prepare", preflight)

    class Group:
        def __init__(self, *args):
            self.state = "Uninitialized"

        def Start(self):
            self.state = "Started"
            actions.append("group_start")
            return self.state

        def GetStatus(self):
            return self.state

        def RollBack(self):
            actions.append("group_rollback")
            self.state = "RolledBack"
            if fault == "netted":
                event = NS(Operation="TransactionGroupRolledBack", GetDocument=lambda: document,
                           GetTransactionNames=lambda: [module["TRANSACTION_NAME"]],
                           GetAddedElementIds=Collection, GetModifiedElementIds=Collection,
                           GetDeletedElementIds=lambda: Collection([NS(Value=20)]))
                document.Application.DocumentChanged.callback(None, event)
            if fault != "survives":
                objects.clear()
            return self.state

    class Transaction(Group):
        def RollBack(self):
            actions.append("transaction_rollback")
            self.state = "RolledBack"
            return self.state

        def Commit(self):
            self.state = "Pending" if fault == "pending" else "Committed"
            if self.state == "Pending":
                return self.state
            if fault == "added":
                objects["21"] = object()
            event = NS(Operation="TransactionCommitted",
                       GetDocument=lambda: object() if fault == "foreign" else document,
                       GetTransactionNames=lambda: [module["TRANSACTION_NAME"]],
                       GetAddedElementIds=lambda: Collection(NS(Value=int(value)) for value in objects),
                       GetModifiedElementIds=lambda: Collection([NS(Value=10), NS(Value=11)]),
                       GetDeletedElementIds=Collection)
            document.Application.DocumentChanged.callback(None, event)
            return self.state

    def place(_, _reader, _route, _types, _original, created, _clock, _budget):
        objects["20"] = object()
        created.add("20")
        if fault == "regen":
            raise RegenerationFailure()
        return ([20], [], "diameter")

    def measure(*_):
        actions.append("measure")
        if fault == "envelope":
            raise ValueError("A trial route envelope extends outside the reviewed routing area.")
        return [{"id": "20"}]

    db.TransactionGroup, db.Transaction = Group, Transaction
    monkeypatch.setitem(module, "place", place)
    monkeypatch.setitem(module, "verify_route", measure)
    if fault == "wrong_review":
        with pytest.raises(ValueError, match="preflight does not match"):
            module["run"](document, args, reader)
        assert actions == ["preflight"]
        return

    result = module["run"](document, args, reader)
    assert result["status"] == expected, result
    assert not hasattr(document.Application.DocumentChanged, "callback")
    if fault == "pending":
        assert "group_rollback" not in actions
        assert not any(isinstance(action, tuple) for action in actions)
    elif fault == "regen":
        assert actions[-2:] == ["transaction_rollback", "group_rollback"]
        assert not any(isinstance(action, tuple) for action in actions)
    else:
        assert "group_rollback" in actions
    if fault is None:
        assert result["receipt"]["changes"] == {"added": ["20"], "modified": ["10", "11"], "deleted": []}
        assert actions[-2:] == [("resolve", "20"), "preflight"]
    if fault == "netted":
        assert result["receipt"]["rollback_events"][0]["deleted"] == ["20"]
    if fault == "added":
        assert result["observed_additions"] == ["20", "21"]
        assert "measure" not in actions


@pytest.mark.parametrize("shifted", [False, True])
def test_fitted_straight_route_rechecks_geometry_and_physical_neighbors(native, shifted):
    from copy import deepcopy

    db, module = native
    db.BuiltInParameter = NS(RBS_DUCT_SYSTEM_TYPE_PARAM="system")
    identity = lambda number: NS(Value=number)
    curve = NS(Id=identity(20), UniqueId="curve-20", GetTypeId=lambda: identity(30),
               ReferenceLevel=NS(Id=identity(31), UniqueId="level-31"),
               get_Parameter=lambda name: (NS(HasValue=True, AsDouble=lambda: 1) if name == "diameter"
                                            else NS(HasValue=True, AsElementId=lambda: identity(32))),
               get_BoundingBox=lambda _: NS(Min=[0, -0.5, -0.5], Max=[4, 0.5, 0.5]))
    owner_a, owner_b = (NS(Id=identity(value), UniqueId=f"owner-{value}") for value in (10, 11))

    def port(owner, number, origin):
        item = NS(Owner=owner, Id=number, Origin=origin, ConnectorType="End", Shape="Round",
                  Radius=0.5, IsConnected=True, AllRefs=[])
        item.IsConnectedTo = lambda other: any(ref is other for ref in item.AllRefs)
        return item

    left, right = port(curve, 1, [0, 0, 0]), port(curve, 2, [4, 1 if shifted else 0, 0])
    start, end = port(owner_a, 3, [0, 0, 0]), port(owner_b, 4, [4, 0, 0])
    for a, b in ((left, start), (right, end)):
        a.AllRefs, b.AllRefs = [b], [a]
    curve.ports, owner_a.ports, owner_b.ports = [left, right], [start], [end]
    document = NS(GetElement=lambda item: {10: owner_a, 11: owner_b, 20: curve,
                                           30: NS(UniqueId="type-30")}[item.Value])
    owners = [{"min_ft": [-1, -1, -1], "max_ft": [0, 1, 1], "connectors": [{"id": 3, "references": []}]},
              {"min_ft": [4, -1, -1], "max_ft": [5, 1, 1], "connectors": [{"id": 4, "references": []}]}]
    original = {"owners": owners, "systems": [], "endpoints": [
        {"element_id": "10", "element_uid": "owner-10", "connector_id": 3},
        {"element_id": "11", "element_uid": "owner-11", "connector_id": 4},
    ]}
    live = deepcopy(original)
    live["owners"][0]["connectors"][0]["references"] = [{"id": "20"}]
    live["owners"][1]["connectors"][0]["references"] = [{"id": "20"}]
    reader = NS(check_time=lambda _: None, route_visit=lambda *_: None, xyz=list,
                connector_collection=lambda item: item.ports, element_id=lambda value: identity(int(value)),
                resolve_source=lambda *_: (document, None, None), read_route_trial_owners=lambda *_, **__: live)
    route = {"points_ft": [[0, 0, 0], [4, 0, 0]], "bends_degrees": []}
    types = {"fitting": {"diameter_ft": 1, "domain": "DomainHvac", "curve_type_id": "30",
                         "curve_type_uid": "type-30", "elbow": None},
             "system_type": {"id": "32"}, "level": {"id": "31", "uid": "level-31"}}
    data = {"clearance_ft": 0.1, "routing_bounds_ft": {"min_ft": [-1, -1, -1], "max_ft": [5, 1, 1]}}
    args = document, reader, route, types, original, data, ([identity(20)], [], "diameter"), {"20"}, None, {}
    if shifted:
        with pytest.raises(ValueError, match="displaced or resized"):
            module["verify_route"](*args)
    else:
        measured, = module["verify_route"](*args)
        assert measured["ends_ft"] == route["points_ft"]
        assert measured["bounds_ft"] == [[0, -0.5, -0.5], [4, 0.5, 0.5]]
