import hashlib
import json
import os
from pathlib import Path
import runpy
import struct
import subprocess
import sys
import zlib

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

def test_thumbnail_fingerprint_excludes_wrapper_and_checks_crc(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    inspect = runpy.run_path(str(SCRIPT))["thumbnail_png"]
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b""))
    expected = {"sha256": hashlib.sha256(png).hexdigest(), "width": 1, "height": 1}
    assert inspect(b"revit header" + png + b"trailer") == expected
    corrupt = bytearray(png)
    corrupt[30] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        inspect(bytes(corrupt))
    with pytest.raises(ValueError, match="incomplete"):
        inspect(png[:-12])


def test_batch_inspects_each_manifest_output_and_reports_incomplete_coverage(folders, monkeypatch, capsys):
    source, _, output = folders
    names = ("Desk.rfa", "Chair.rfa")
    for name in names:
        (source / name).write_bytes(name.encode("ascii"))
    staged = stage(folders, *names)
    assert staged.returncode == 0, staged.stderr
    manifest = json.loads(staged.stdout)["manifest"]
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    main = runpy.run_path(str(SCRIPT))["main"]

    def read_thumbnail(path):
        if path.parent != output or path.read_bytes() == b"no thumbnail":
            return None
        return {"width": 1, "height": 1, "sha256": "a" * 64}

    monkeypatch.setitem(main.__globals__, "embedded_thumbnail", read_thumbnail)
    (output / "Desk.rfa").write_bytes(b"saved desk")
    command = ["inspect-output", manifest]
    assert main(command) == 1
    partial = json.loads(capsys.readouterr().out)
    assert partial["status"] == "incomplete"
    by_name = {row["name"]: row for row in partial["families"]}
    assert by_name["Chair.rfa"]["status"] == "unavailable"
    assert by_name["Desk.rfa"]["thumbnail_status"] == "present"

    (output / "Chair.rfa").write_bytes(b"no thumbnail")
    assert main(command) == 1
    partial = json.loads(capsys.readouterr().out)
    by_name = {row["name"]: row for row in partial["families"]}
    assert by_name["Chair.rfa"]["thumbnail_status"] == "missing"

    (output / "Chair.rfa").write_bytes(b"saved chair")
    assert main(command) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["status"] == "thumbnail_review_pending"
    assert [(row["name"], row["thumbnail_status"]) for row in ready["families"]] == [
        ("Desk.rfa", "present"), ("Chair.rfa", "present"),
    ]

    assert main(command + ["--family", "Chair.rfa"]) == 0
    assert json.loads(capsys.readouterr().out) == ready["families"][1]
