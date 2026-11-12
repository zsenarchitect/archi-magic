import json
import math
from pathlib import Path
import runpy
import subprocess
import sys
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from level_alignment_preview import compute, rotation_delta


def instance(id_value, **updates):
    return dict({
        "id": id_value, "uid": "instance-" + id_value, "type_uid": "type-a",
        "model_point": True, "editable": True, "supported": True,
        "point": [2, 3, 110], "rotation_rad": 0.0,
        "level_uid": "lower", "level_parameter_uid": "lower",
        "level_z_ft": 100, "offset_ft": 10, "offset_writable": True, "level_writable": True,
        "offset_contract": {
            "level_parameter_id": "level", "offset_parameter_id": "elevation",
            "length_spec": "length", "binding": "instance",
            "level_storage": "ElementId", "offset_storage": "Double",
        },
    }, **updates)


def snapshot(operation, targets, **updates):
    return dict({
        "kind": "level-alignment-snapshot", "version": 1,
        "project": {"project_uid": "project", "path_sha256": "0" * 64},
        "operation": operation, "targets": targets, "frame_verified": True,
    }, **updates)


@pytest.mark.parametrize(("current", "reference", "expected"), [(350, 10, 20), (10, 350, -20), (0, 180, -180)])
def test_shortest_rotation(current, reference, expected):
    assert rotation_delta(math.radians(current), math.radians(reference)) == pytest.approx(math.radians(expected))
    target = instance("101", rotation_rad=math.radians(current))
    plan = compute(snapshot("rotation", [target], reference=instance("102", rotation_rad=math.radians(reference))))
    assert plan["after"] == [["101", 2, 3, 110, pytest.approx(math.radians(reference)), "lower", 10]]
    assert plan["ownership_ids"] == ["101"]
    assert plan["steps"] == []
    assert plan["review_only"] is True


def test_offset_match_uses_the_target_level_and_preserves_xy_and_rotation():
    reference = instance("102", point=[4, 5, 108], level_z_ft=111, offset_ft=-3)
    target = instance("101", rotation_rad=0.2)
    plan = compute(snapshot("offset", [target], reference=reference))
    assert plan["after"] == [["101", 2, 3, 97, 0.2, "lower", -3]]
    assert plan["ownership_ids"] == ["101"]

    target["offset_contract"]["offset_parameter_id"] = "different"
    blocked = compute(snapshot("offset", [target], reference=reference))
    assert blocked["blocked"]
    assert blocked["after"] == blocked["before"]
    assert blocked["ownership_ids"] == []


def test_relevel_preserves_absolute_location_in_a_shifted_frame():
    levels = [{"uid": "lower", "elevation_ft": 100}, {"uid": "upper", "elevation_ft": 112}]
    target = instance("101")
    unchanged = instance("102", point=[2, 3, 101], offset_ft=1)
    basis = snapshot("nearest_level", [target, unchanged], levels=levels, levels_complete=True)
    plan = compute(basis)
    assert plan["after"] == [["101", 2, 3, 110, 0.0, "upper", -2], ["102", 2, 3, 101, 0.0, "lower", 1]]
    assert plan["ownership_ids"] == ["101"]
    assert compute(dict(reversed(list(basis.items()))))["basis_sha256"] == plan["basis_sha256"]
    assert compute(dict(basis, levels=list(reversed(levels))))["basis_sha256"] != plan["basis_sha256"]


@pytest.mark.parametrize("other_level", [100, 120, 120.0005])
def test_tie_blocks_before_already_assigned_no_op(other_level):
    levels = [{"uid": "lower", "elevation_ft": 100}, {"uid": "upper", "elevation_ft": other_level}]
    plan = compute(snapshot("nearest_level", [instance("101")], levels=levels, levels_complete=True))
    assert plan["blocked"]
    assert "tie tolerance" in plan["warnings"][0]["text"]
    assert plan["ownership_ids"] == []


def test_a_blocked_row_clears_the_batch_write_scope():
    plan = compute(snapshot("rotation", [instance("101"), instance("102", model_point=None)],
                            reference=instance("103", rotation_rad=0.5)))
    assert plan["blocked"]
    assert plan["warnings"][0]["row"] == "102"
    assert plan["ownership_ids"] == []


def test_cli_blocks_unverified_frames_and_incomplete_level_inventory(tmp_path):
    path = tmp_path / "snapshot.json"
    basis = snapshot("nearest_level", [instance("101")], frame_verified=False,
                     levels=[{"uid": "lower", "elevation_ft": 100}], levels_complete=True)
    command = [sys.executable, str(SCRIPTS / "level_alignment_preview.py"), str(path)]
    path.write_text(json.dumps(basis))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["ownership_ids"] == []

    path.write_text(json.dumps(dict(basis, levels_complete=False)))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2
    assert "complete bounded inventory" in result.stderr


def test_legacy_transform_writer_rejects_rendered_preview(monkeypatch):
    preview = compute(snapshot("rotation", [instance("101")], reference=instance("102", rotation_rad=0.5)))
    api = ModuleType("pyrevit.api")
    api.DB = object()
    monkeypatch.setitem(sys.modules, "pyrevit.api", api)
    with pytest.raises(ValueError, match="no element-transform steps"):
        runpy.run_path(str(SCRIPTS / "apply_transforms.py"), init_globals={"inputs": {"preview": preview}})
