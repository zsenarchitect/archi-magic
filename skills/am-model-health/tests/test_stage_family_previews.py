import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "stage_family_previews.py"


@pytest.fixture
def folders(tmp_path):
    result = [tmp_path.resolve() / name for name in ("source", "stage", "output")]
    for path in result:
        path.mkdir()
    return result


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)


def stage(folders, *names):
    source, workspace, output = folders
    options = [argument for name in names for argument in ("--family", name)]
    return run("stage", source, "--staging-parent", workspace, "--output", output, *options)


def test_stage_selected_families_and_verify_before_and_after_one_save(folders):
    source, staging, output = folders
    (source / "Model.rfa").write_bytes(b"synthetic model")
    (source / "Other.0001.rfa").write_bytes(b"synthetic second model")
    inventory = json.loads(run("inventory", source).stdout)
    assert [(row["name"], row["backup_candidate"]) for row in inventory["families"]] == [
        ("Model.rfa", False), ("Other.0001.rfa", True),
    ]
    result = stage(folders, "Model.rfa", "Other.0001.rfa")
    assert result.returncode == 0, result.stderr
    path = Path(json.loads(result.stdout)["manifest"])
    assert path.parent.parent == staging
    assert (path.parent / "Model.rfa").read_bytes() == (source / "Model.rfa").read_bytes()
    before = run("verify", path)
    assert before.returncode == 0, before.stderr
    assert json.loads(before.stdout)["families"] == [
        {"name": "Model.rfa", "source": "verified", "staged": "verified", "output": "available"},
        {"name": "Other.0001.rfa", "source": "verified", "staged": "verified", "output": "available"},
    ]
    (path.parent / "Model.rfa").write_bytes(b"changed working copy")
    (output / "MODEL.RFA").write_bytes(b"saved preview")
    report = run("verify", path)
    assert report.returncode == 1
    assert json.loads(report.stdout)["families"][0] == {
        "name": "Model.rfa", "source": "verified", "staged": "changed or unavailable", "output": "conflict",
    }
    next_family = run("verify", path, "--family", "Other.0001.rfa")
    assert next_family.returncode == 0, next_family.stderr
    staged = path.parent / "Other.0001.rfa"
    assert json.loads(next_family.stdout)["revit_inputs"] == {
        "staged_path_sha256": hashlib.sha256(os.path.normcase(str(staged)).encode()).hexdigest(),
        "staged_file_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
    }
    assert "revit_inputs" not in json.loads(run("verify", path, "--family", "Model.rfa").stdout)
    assert run("verify", path, "--source-only").returncode == 0
    original = source / "Model.rfa"
    moved = source / "previous.bin"
    original.rename(moved)
    original.write_bytes(moved.read_bytes())
    final = run("verify", path, "--source-only")
    assert final.returncode == 1
    assert json.loads(final.stdout)["families"][0]["source"] == "changed or unavailable"


def test_stage_rejects_source_alias_output_collision_and_traversal(folders):
    source, staging, output = folders
    original = source / "Model.rfa"
    original.write_bytes(b"synthetic model")
    (source / "Alias.rfa").hardlink_to(original)
    listing = json.loads(run("inventory", source).stdout)
    assert [row["status"] for row in listing["families"]] == ["refused", "refused"]
    assert stage(folders, "Model.rfa").returncode == 2
    (source / "Alias.rfa").unlink()
    (output / "MODEL.RFA").write_bytes(b"existing")
    assert stage(folders, "Model.rfa").returncode == 2
    assert stage(folders, "../source/Model.rfa").returncode == 2
    assert not list(staging.iterdir())
    assert (output / "MODEL.RFA").read_bytes() == b"existing"
