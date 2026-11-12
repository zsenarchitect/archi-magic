from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from family_library_discovery import discover, manifest_digest
import stage_family_library as staging


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Native Windows staging needs a Windows fixture.")


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "library"
    destination = tmp_path.resolve() / "staging"
    root.mkdir()
    destination.mkdir()
    (root / "Fixture.rfa").write_bytes(b"reviewed family")
    (root / "Fixture.txt").write_bytes(b"reviewed catalog")
    (root / "Other.rfa").write_bytes(b"not selected")
    monkeypatch.setattr(staging, "free_bytes", lambda parent: 10 * staging.HEADROOM)
    return root, destination


def test_staged_copy_preserves_reviewed_pair_and_private_paths(library):
    root, destination = library
    manifest = discover(str(root))
    receipt = staging.stage(manifest, ["Fixture.rfa"], str(destination))
    assert receipt["discovery_sha256"] == manifest["manifest_sha256"]
    assert len(receipt["pairs"]) == 1
    pair = receipt["pairs"][0]
    workspace = Path(receipt["workspace"])
    staged = workspace / pair["directory"]
    assert workspace.parent == destination
    assert staged.is_dir() and stat.S_IMODE(staged.stat().st_mode) == 0o700
    assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
    assert {item.name for item in staged.iterdir()} == {"Fixture.rfa", "Fixture.txt"}
    for name, expected in (("Fixture.rfa", pair["rfa"]), ("Fixture.txt", pair["catalog"])):
        assert hashlib.sha256((staged / name).read_bytes()).hexdigest() == expected["sha256"]
        assert (staged / name).read_bytes() == (root / name).read_bytes()
        assert stat.S_IMODE((staged / name).stat().st_mode) == 0o600


@pytest.mark.parametrize("mutation", ["rfa_changed", "catalog_changed", "catalog_added", "catalog_removed"])
def test_a_changed_source_pair_cannot_publish_a_receipt(library, mutation):
    root, destination = library
    catalog = root / "Fixture.txt"
    if mutation == "catalog_added":
        catalog.unlink()
    manifest = discover(str(root))
    if mutation == "rfa_changed":
        (root / "Fixture.rfa").write_bytes(b"changed")
    elif mutation in ("catalog_changed", "catalog_added"):
        catalog.write_bytes(b"changed")
    else:
        catalog.unlink()
    with pytest.raises(ValueError, match="catalog.*changed|catalog appeared"):
        staging.stage(manifest, ["Fixture.rfa"], str(destination))


def test_rejects_tampered_manifest_and_destination_inside_library(library):
    root, destination = library
    manifest = discover(str(root))
    with pytest.raises(ValueError, match="outside the family library"):
        staging.stage(manifest, ["Fixture.rfa"], str(root))
    with pytest.raises(ValueError, match="unavailable"):
        staging.stage(manifest, ["../Fixture.rfa"], str(destination))
    manifest["files"][0]["rfa"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="intact discovery"):
        staging.stage(manifest, ["Fixture.rfa"], str(destination))
    assert list(destination.iterdir()) == []


def test_insufficient_headroom_stops_before_writing(library, monkeypatch):
    root, destination = library
    monkeypatch.setattr(staging, "free_bytes", lambda parent: staging.HEADROOM)
    with pytest.raises(ValueError, match="free space"):
        staging.stage(discover(str(root)), ["Fixture.rfa"], str(destination))
    assert list(destination.iterdir()) == []


def test_replacement_directory_cannot_stage_matching_bytes(library):
    root, destination = library
    folder = root / "sub"
    folder.mkdir()
    (folder / "Fixture.rfa").write_bytes(b"same")
    manifest = discover(str(root))
    folder.rename(root / "old")
    folder.mkdir()
    (folder / "Fixture.rfa").write_bytes(b"same")
    with pytest.raises(ValueError, match="ancestry changed"):
        staging.stage(manifest, ["sub/Fixture.rfa"], str(destination))


def test_malformed_catalog_basename_cannot_escape_the_pair_folder(library):
    root, destination = library
    manifest = discover(str(root))
    manifest["files"][0]["catalog"]["basename"] = "../Fixture.txt"
    del manifest["manifest_sha256"]
    manifest["manifest_sha256"] = manifest_digest(manifest)
    with pytest.raises(ValueError, match="catalog basename"):
        staging.stage(manifest, ["Fixture.rfa"], str(destination))
    assert list(destination.iterdir()) == []


def test_cli_returns_no_receipt_for_a_stale_catalog(library, tmp_path):
    root, destination = library
    manifest_file = tmp_path / "discovery.json"
    manifest_file.write_text(json.dumps(discover(str(root))))
    (root / "Fixture.txt").write_bytes(b"new")
    command = [sys.executable, str(SCRIPTS / "stage_family_library.py"),
               str(manifest_file), str(destination), "--path", "Fixture.rfa"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "catalog changed" in result.stderr


@pytest.mark.parametrize(("mutation", "message"), [("bytes", "file bytes differ"), ("catalog", "unexpected files")])
def test_receipt_requires_the_private_copies_to_match_the_source(library, monkeypatch, mutation, message):
    root, destination = library
    original = staging.new_file

    @contextmanager
    def changed_copy(parent, name):
        with original(parent, name) as stream:
            yield stream
        if mutation == "bytes":
            descriptor = os.open(name, os.O_WRONLY | os.O_TRUNC, dir_fd=parent)
        else:
            descriptor = os.open("Other.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"unapproved")

    monkeypatch.setattr(staging, "new_file", changed_copy)
    with pytest.raises(ValueError, match=message):
        staging.stage(discover(str(root)), ["Other.rfa"], str(destination))
