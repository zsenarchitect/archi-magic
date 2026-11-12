import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from mep_penetration_spacing import spacing_conflicts


def round_opening(key, x, y=0):
    return {"key": key, "shape": "round", "center": [x, y], "diameter": 2}


def rectangle(key, x, y=0, angle=0):
    return {"key": key, "shape": "rectangular", "center": [x, y], "width": 2, "height": 2,
            "angle_rad": angle}


@pytest.mark.parametrize("neighbor, expected", [
    (round_opening("second", 1.5), 0),
    (round_opening("second", 2.25), 0.25),
    (rectangle("second", 2.25), 0.25),
    (rectangle("second", 2.5, angle=math.pi / 4), 1.5 - math.sqrt(2)),
])
def test_spacing_rejects_openings_that_overlap_or_leave_too_little_material(neighbor, expected):
    first = round_opening("first", 0)
    rows = spacing_conflicts([first, neighbor], [], 0.5)
    assert len(rows) == 1
    assert rows[0] == {"opening": "first", "neighbor": "second", "gap_ft": pytest.approx(expected)}


def test_rotated_rectangles_use_outline_distance_instead_of_bounding_boxes():
    first = rectangle("first", 0, angle=math.pi / 4)
    neighbor = rectangle("second", 2.5, 2.5, angle=math.pi / 4)
    assert spacing_conflicts([first], [neighbor], 1.0) == []
    assert spacing_conflicts([first], [neighbor], 2.0)[0]["gap_ft"] == pytest.approx(2.5 * math.sqrt(2) - 2)


def test_exact_web_is_allowed_and_existing_only_pairs_are_out_of_scope():
    first = rectangle("first", 0)
    assert spacing_conflicts([first], [rectangle("existing", 2.5)], 0.5) == []
    assert spacing_conflicts([], [first, rectangle("existing", 0.5)], 0.5) == []
    assert spacing_conflicts([first], [rectangle("existing", 0.5)], 0.5)[0]["gap_ft"] == 0


def test_invalid_geometry_and_duplicate_keys_do_not_produce_a_proposal():
    first = rectangle("first", 0)
    with pytest.raises(ValueError, match="unique key"):
        spacing_conflicts([first], [first], 0.5)
    with pytest.raises(ValueError, match="positive minimum"):
        spacing_conflicts([first], [], 0)
    with pytest.raises(ValueError, match="finite"):
        spacing_conflicts([rectangle("bad", float("nan"))], [], 0.5)


def test_cli_rejects_incomplete_scope_and_returns_gap_for_complete_scope(tmp_path):
    import json
    import subprocess

    evidence = {"kind": "mep-penetration-host-spacing", "frame": "host_face_internal_feet", "host_uid": "host-20",
                "complete": False, "minimum_web_ft": 0.5,
                "proposed": [round_opening("proposal", 0)], "existing": [round_opening("existing", 2.25)]}
    path = tmp_path / "scope.json"
    script = Path(__file__).resolve().parents[1] / "scripts" / "mep_penetration_spacing.py"
    command = [sys.executable, str(script), str(path)]
    path.write_text(json.dumps(evidence))
    stopped = subprocess.run(command, capture_output=True, text=True)
    assert stopped.returncode == 2
    assert not stopped.stdout
    evidence["complete"] = True
    path.write_text(json.dumps(evidence))
    conflict = subprocess.run(command, capture_output=True, text=True)
    assert conflict.returncode == 1
    assert json.loads(conflict.stdout) == {
        "host_uid": "host-20", "conflicts": [{"opening": "proposal", "neighbor": "existing", "gap_ft": 0.25}]}
