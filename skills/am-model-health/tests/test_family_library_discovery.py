import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import family_library_discovery as library


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Descriptor-relative discovery is qualified on POSIX.")


def digest(value):
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def root(tmp_path):
    path = tmp_path.resolve() / "library"
    path.mkdir()
    return path


def test_manifest_distinguishes_same_basenames_and_catalog_only_changes(root):
    (root / "Furniture").mkdir()
    (root / "Fixtures").mkdir()
    (root / "Furniture" / "Example.rfa").write_bytes(b"furniture")
    (root / "Fixtures" / "Example.rfa").write_bytes(b"fixtures")
    catalog = root / "Furniture" / "Example.txt"
    catalog.write_bytes(b"catalog-1")

    manifest = library.discover(str(root))
    assert manifest["complete"]
    assert manifest["skipped"] == []
    assert [(item["path"], item["rfa"]["sha256"], item["catalog"]) for item in manifest["files"]] == [
        ("Fixtures/Example.rfa", digest(b"fixtures"), None),
        ("Furniture/Example.rfa", digest(b"furniture"),
         {"basename": "Example.txt", "bytes": 9, "sha256": digest(b"catalog-1")}),
    ]
    assert manifest["files"][0]["ancestors"][0] == manifest["root_identity"]
    assert manifest["files"][0]["ancestors"] != manifest["files"][1]["ancestors"]
    assert library.discover(str(root)) == manifest

    catalog.write_bytes(b"catalog-2")
    changed = library.discover(str(root))
    assert changed["files"][1]["rfa"] == manifest["files"][1]["rfa"]
    assert changed["files"][1]["catalog"]["sha256"] == digest(b"catalog-2")
    assert changed["manifest_sha256"] != manifest["manifest_sha256"]
    catalog.unlink()
    assert library.discover(str(root))["files"][1]["catalog"] is None


def test_directory_and_leaf_links_and_hardlinks_are_excluded(root):
    outside = root.parent / "outside"
    outside.mkdir()
    source = outside / "secret.rfa"
    source.write_bytes(b"outside")
    (root / "folder-link").symlink_to(outside, target_is_directory=True)
    (root / "leaf-link.rfa").symlink_to(source)
    os.link(source, root / "hardlink.rfa")
    (root / "Good.rfa").write_bytes(b"inside")
    manifest = library.discover(str(root))
    assert not manifest["complete"]
    assert [row["path"] for row in manifest["files"]] == ["Good.rfa"]
    assert {row["path"] for row in manifest["skipped"]} == {"folder-link", "leaf-link.rfa", "hardlink.rfa"}
    with pytest.raises(ValueError, match="ordinary directory"):
        library.discover(str(root / "folder-link"))


def test_case_collision_is_never_paired_or_indexed(root, monkeypatch):
    (root / "Example.rfa").write_bytes(b"rfa")
    (root / "Example.txt").write_bytes(b"catalog")
    original = library.classify_entries

    def collision(fd, budget):
        rows = original(fd, budget)
        rows["example.txt"].append(("EXAMPLE.TXT", rows["example.txt"][0][1]))
        return rows

    monkeypatch.setattr(library, "classify_entries", collision)
    manifest = library.discover(str(root))
    assert not manifest["complete"]
    assert manifest["files"] == []
    assert any(row["path"] == "Example.rfa" and "case-colliding" in row["reason"] for row in manifest["skipped"])


def test_ancestor_swap_never_redirects_the_read(root, monkeypatch):
    inside = root / "child"
    inside.mkdir()
    (inside / "Widget.rfa").write_bytes(b"inside")
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "Widget.rfa").write_bytes(b"outside")
    original = library.checked_file
    read_hashes = []

    def swap(parent, name, enumerated, cap, budget):
        inside.rename(root.parent / "moved")
        inside.symlink_to(outside, target_is_directory=True)
        result = original(parent, name, enumerated, cap, budget)
        read_hashes.append(result["sha256"])
        return result

    monkeypatch.setattr(library, "checked_file", swap)
    manifest = library.discover(str(root))
    assert read_hashes == [digest(b"inside")]
    assert manifest["files"] == []
    assert not manifest["complete"]
    assert any("changed" in row["reason"] for row in manifest["skipped"])


def test_leaf_swap_is_rejected_before_reading_bytes(root, monkeypatch):
    leaf = root / "Widget.rfa"
    leaf.write_bytes(b"inside")
    outside = root.parent / "outside.rfa"
    outside.write_bytes(b"outside")
    original = library.checked_file

    def swap(parent, name, enumerated, cap, budget):
        leaf.unlink()
        leaf.symlink_to(outside)
        return original(parent, name, enumerated, cap, budget)

    monkeypatch.setattr(library, "checked_file", swap)
    manifest = library.discover(str(root))
    assert manifest["files"] == []
    assert not manifest["complete"]


def test_limits_preserve_explicit_partial_coverage(root, monkeypatch):
    (root / "A.rfa").write_bytes(b"first")
    (root / "B.rfa").write_bytes(b"second")
    monkeypatch.setattr(library, "MAX_FAMILIES", 1)
    manifest = library.discover(str(root))
    assert [row["path"] for row in manifest["files"]] == ["A.rfa"]
    assert manifest["skipped"] == [{"path": "B.rfa", "reason": "The family file count limit was exceeded."}]
    assert not manifest["complete"]


def test_cli_reports_partial_discovery_as_a_distinct_exit_status(root):
    (root / "empty.rfa").touch()
    result = subprocess.run([sys.executable, str(SCRIPTS / "family_library_discovery.py"), str(root)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["skipped"][0]["path"] == "empty.rfa"


def test_ordinary_directory_replacement_is_rejected_after_enumeration(root, monkeypatch):
    child = root / "child"
    child.mkdir()
    (child / "Widget.rfa").write_bytes(b"original")
    replacement = root.parent / "replacement"
    replacement.mkdir()
    (replacement / "Widget.rfa").write_bytes(b"replacement")
    original = library.classify_entries

    def swap(parent, budget):
        entries = original(parent, budget)
        child.rename(root.parent / "moved")
        replacement.rename(child)
        return entries

    monkeypatch.setattr(library, "classify_entries", swap)
    manifest = library.discover(str(root))
    assert not manifest["complete"]
    assert manifest["files"] == []
    assert manifest["skipped"][0] == {"path": "child", "reason": "The path changed after enumeration."}
