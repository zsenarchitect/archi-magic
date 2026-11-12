import ctypes
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace as NS

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import family_library_discovery as library
import family_library_windows as windows


@pytest.mark.parametrize("attribute", [0x400, 0x1000, 0x40000, 0x80000, 0x100000, 0x400000])
def test_reparse_and_offline_metadata_stop_before_create_file(attribute, monkeypatch):
    attempted = []
    dll = NS(
        GetFileAttributesW=lambda name: attribute,
        CreateFileW=lambda *args: attempted.append(args),
    )
    monkeypatch.setattr(windows, "api", lambda: dll)
    with pytest.raises(ValueError, match="offline, reparse, recall"):
        with windows.open_handle(Path("Widget.rfa"), directory=False, read_bytes=True):
            pytest.fail("The flagged file was opened.")
    assert attempted == []


@pytest.mark.parametrize(("directory", "read_bytes", "access", "share", "extra_flags"), [
    (True, False, windows.READ_ATTRIBUTES, windows.SHARE_READ | windows.SHARE_WRITE, windows.BACKUP_SEMANTICS),
    (False, False, windows.READ_ATTRIBUTES, windows.SHARE_READ, 0),
    (False, True, windows.GENERIC_READ, windows.SHARE_READ, 0),
])
def test_windows_open_pins_ancestors_and_uses_no_follow_and_no_recall(
        directory, read_bytes, access, share, extra_flags, monkeypatch):
    attributes = windows.DIRECTORY if directory else 0x20
    opened, closed = [], []
    dll = NS(
        GetFileAttributesW=lambda name: attributes,
        CreateFileW=lambda *args: opened.append(args) or 21,
        CloseHandle=lambda handle: closed.append(handle) or True,
    )
    monkeypatch.setattr(windows, "api", lambda: dll)
    monkeypatch.setattr(windows, "handle_info", lambda handle: NS(st_file_attributes=attributes))
    with windows.open_handle(Path("Library"), directory=directory, read_bytes=read_bytes) as (handle, _):
        assert handle == 21
        assert closed == []
    assert opened == [("Library", access, share, None, windows.OPEN_EXISTING,
                       windows.OPEN_REPARSE_POINT | windows.OPEN_NO_RECALL | extra_flags, None)]
    assert closed == [21]


def test_handle_identity_uses_volume_and_all_128_bits(monkeypatch):
    identifier = bytes(range(16))

    def file_info(handle, kind, output, size):
        assert handle == 11
        structure = {0: windows.BasicInfo, 1: windows.StandardInfo, 18: windows.FileIdInfo}[kind]
        assert size == ctypes.sizeof(structure)
        info = ctypes.cast(output, ctypes.POINTER(structure)).contents
        if kind == 0:
            info.attributes, info.write, info.change = 0x20, 123, 456
        elif kind == 1:
            info.links, info.size, info.directory = 1, 17, 0
        else:
            info.volume, info.identifier[:] = 55, identifier
        return True

    monkeypatch.setattr(windows, "api", lambda: NS(GetFileType=lambda handle: 1,
                                                GetFileInformationByHandleEx=file_info))
    info = windows.handle_info(11)
    assert (info.st_dev, info.st_ino) == (55, int.from_bytes(identifier, "little"))
    assert (info.st_mode, info.st_size, info.st_nlink) == (stat.S_IFREG, 17, 1)
    assert (info.st_mtime_ns, info.st_ctime_ns) == (12300, 45600)


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows handle sharing.")
def test_ordinary_windows_library_is_read_without_releasing_ancestry(tmp_path):
    root = tmp_path.resolve() / "library"
    root.mkdir()
    family = root / "Widget.rfa"
    family.write_bytes(b"fixture")
    (root / "Widget.txt").write_bytes(b"catalog")

    with windows.open_handle(root, directory=True):
        with pytest.raises(OSError):
            root.rename(root.with_name("moved"))
    with windows.open_handle(family, directory=False, read_bytes=True):
        with pytest.raises(OSError):
            family.write_bytes(b"unexpected replacement")
    manifest = library.discover(str(root))
    assert manifest["complete"]
    assert manifest["files"][0]["rfa"]["bytes"] == 7
    assert manifest["files"][0]["catalog"]["basename"] == "Widget.txt"
