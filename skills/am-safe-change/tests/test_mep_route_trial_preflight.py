from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS

import pytest

from test_mep_route_preview import fixture


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def reviewed_fixture(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import mep_route_preview

    data = fixture()
    host = {"project_uid": "host", "path_sha256": "a" * 64}
    source = {"host_identity": host, "document_identity": host, "link_instance_id": None,
              "link_instance_uid": None, "transform": [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]}
    data["endpoints"]["source"] = source
    data["routing_bounds_ft"] = {"min_ft": [-1, -3, -2], "max_ft": [11, 3, 2]}
    args = {
        "disposable_fixture": True, "empty_obstacle_scope": True,
        "owners_record": {"kind": "mep-route-trial-owners", "version": 1, "source": source,
                          "snapshot": {"endpoints": data["endpoints"]["endpoints"], "systems": [],
                                       "owners": [{"element_id": "41", "element_uid": "first"},
                                                  {"element_id": "42", "element_uid": "last"}]}},
        "types_record": {"kind": "mep-route-trial-types", "version": 1, "source": source, "types": {
            "level": {"id": "45", "uid": "level"},
            "system_type": {"id": "44", "uid": "system", "classification": "SupplyAir"},
            "fitting": {"curve_type_id": "43", "curve_type_uid": "curve", "diameter_ft": 1,
                        "domain": "DomainHvac", "elbow": {"id": "46", "uid": "elbow"}},
        }},
    }

    def review():
        args["route_input_json"] = json.dumps(data)
        preview = mep_route_preview.preview(data)
        preview["basis_sha256"] = sha256(args["route_input_json"].encode()).hexdigest()
        args["preview"] = json.loads(json.dumps(preview))

    review()
    document = NS(PathName="C:/Tests/disposable.rvt", IsWorkshared=False, IsReadOnly=False,
                  IsModifiable=False, Application=NS(VersionNumber="2026"))
    return data, args, review, document


def read_owner_types(args):
    def query(document, request):
        if "route_trial_owners" in request:
            assert len(request["route_trial_owners"]) == 2
            assert request["route_trial_owners"][0] == {
                "element_id": "41", "element_uid": "first", "connector_id": 1,
            }
            return {"status": "ok", "trial_owners_json": json.dumps(args["owners_record"])}
        if "route_trial_types" in request:
            assert request["route_trial_types"]["system_type"] == {"id": "44", "uid": "system"}
            return {"status": "ok", "trial_types_json": json.dumps(args["types_record"])}
        pytest.fail("Unexpected request")

    return query


def test_installed_bridge_entry_binds_review_and_fresh_evidence(reviewed_fixture, monkeypatch):
    _, args, _, document = reviewed_fixture
    issue_scripts = SCRIPTS.parents[1] / "am-issue-board" / "scripts"
    reader = NS(__file__=str(issue_scripts / "read_mep.py"), run_query=read_owner_types(args),
                ib=NS(__file__=str(issue_scripts / "issue_board.py")))
    monkeypatch.setitem(sys.modules, "read_mep", reader)
    result = runpy.run_path(str(SCRIPTS / "mep_route_trial_preflight.py"),
                            init_globals={"doc": document, "inputs": args})["result"]
    assert result["status"] == "ok", result
    receipt = json.loads(result["preflight_json"])
    assert receipt["source"] == args["owners_record"]["source"]
    assert receipt["route_input_sha256"] == args["preview"]["basis_sha256"]
    assert len(receipt["owners_sha256"]) == len(receipt["types_sha256"]) == 64


@pytest.mark.parametrize("fault, expected", [
    ("review", "does not match"), ("manual_boxes", "native obstacles"),
    ("missing_scope", "empty scope"), ("workshared", "standalone"),
    ("stale_owners", "current fixture"), ("stale_types", "current fixture"),
    ("stale_obstacle", "current fixture"),
])
def test_route_preflight_rejects_unreviewed_or_stale_inputs(reviewed_fixture, fault, expected):
    data, args, review, document = reviewed_fixture
    from mep_route_trial_preflight import prepare

    if fault == "review":
        args["preview"]["after"][0][1] = 11
    elif fault == "manual_boxes":
        data["obstacles"] = [{"min_ft": [4, -1, -1], "max_ft": [6, 1, 1]}]
        review()
    elif fault == "missing_scope":
        del args["empty_obstacle_scope"]
    elif fault == "workshared":
        document.IsWorkshared = True
    elif fault == "stale_obstacle":
        data["obstacle_records"] = [{
            "kind": "mep-route-obstacles", "version": 1, "source": data["endpoints"]["source"],
            "obstacles": [{"element_id": "47", "element_uid": "obstacle", "min_ft": [4, 2, 0],
                           "max_ft": [6, 3, 1]}],
        }]
        review()

    query = read_owner_types(args)

    def changed(doc, request):
        if "route_obstacles" in request:
            stale = deepcopy(data["obstacle_records"][0])
            stale["obstacles"][0]["min_ft"][1] = 2.1
            return {"status": "ok", "obstacles_json": json.dumps(stale)}
        response = query(doc, request)
        if fault == "stale_owners" and "trial_owners_json" in response:
            owners = json.loads(response["trial_owners_json"])
            owners["snapshot"]["systems"] = [{"id": "51"}]
            response["trial_owners_json"] = json.dumps(owners)
        if fault == "stale_types" and "trial_types_json" in response:
            types = json.loads(response["trial_types_json"])
            types["types"]["level"]["uid"] = "different"
            response["trial_types_json"] = json.dumps(types)
        return response

    with pytest.raises(ValueError, match=expected):
        prepare(document, args, reader=NS(run_query=changed))


def test_independent_trial_verifier_requires_exact_complete_bridge_evidence(reviewed_fixture, tmp_path):
    import subprocess
    from mep_route_trial_preflight import TRANSACTION_NAME
    from test_view_filter_graphics import runtime_record

    baseline = {"kind": "mep-route-trial-preflight", "version": 1, "owner_ids": ["41", "42"]}
    query = {"status": "ok", "preflight_json": json.dumps(baseline)}
    changes = {"added": ["63"], "modified": ["41", "42"], "deleted": []}
    measured = {"status": "mep_route_trial_rolled_back", "receipt": {
        "preflight": baseline, "changes": changes, "measurements": [{"id": "63"}], "rollback_events": [],
    }}
    original = runtime_record("a00000000001", "query", query)
    trial = runtime_record("a00000000002", "dry_run", measured, changes["modified"],
                           added=changes["added"], transaction_name=TRANSACTION_NAME)
    fresh = runtime_record("a00000000003", "query", dict(query, confirmed_absent_ids=["63"]))
    paths = [tmp_path / name for name in ("original.json", "trial.json", "fresh.json")]
    cli = [sys.executable, str(SCRIPTS / "mep_route_trial_preflight.py"), *map(str, paths)]

    def verify(records):
        for path, value in zip(paths, records):
            path.write_text(json.dumps(value))
        return subprocess.run(cli, capture_output=True, text=True)

    accepted = verify([original, trial, fresh])
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["changes"] == changes
    bad_fresh = dict(fresh, result=dict(query, confirmed_absent_ids=["64"]))
    assert verify([original, trial, bad_fresh]).returncode == 2
    truncated = deepcopy(trial)
    truncated["changes"]["added"] = []
    assert verify([original, truncated, fresh]).returncode == 2
    assert verify([original, trial, dict(fresh, run_id=original["run_id"])]).returncode == 2

    netted = deepcopy(measured)
    netted["receipt"]["rollback_events"] = [{
        "operation": "TransactionGroupRolledBack", "transactions": [TRANSACTION_NAME],
        "added": [], "modified": ["41", "42"], "deleted": ["63"],
    }]
    net = dict(changes, added=[])
    netted_trial = runtime_record("a00000000002", "dry_run", netted, net["modified"],
                                  added=net["added"], transaction_name=TRANSACTION_NAME)
    accepted = verify([original, netted_trial, fresh])
    assert accepted.returncode == 0, accepted.stderr
    assert json.loads(accepted.stdout)["bridge_changes"] == net
    netted["receipt"]["rollback_events"][0]["deleted"] = ["64"]
    unexpected = verify([original, netted_trial, fresh])
    assert unexpected.returncode == 2 and "unexpected element identities" in unexpected.stderr
