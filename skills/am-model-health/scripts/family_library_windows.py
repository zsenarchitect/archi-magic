"""Windows no-follow handles for ordinary, resident family-library paths."""

from contextlib import contextmanager
import ctypes
from functools import lru_cache
import os
import stat
from types import SimpleNamespace

READ_ATTRIBUTES = 0x80
GENERIC_READ = 0x80000000
SHARE_READ = 1
SHARE_WRITE = 2
OPEN_EXISTING = 3
OPEN_NO_RECALL = 0x00100000
OPEN_REPARSE_POINT = 0x00200000
BACKUP_SEMANTICS = 0x02000000
DIRECTORY = 0x10
INVALID_ATTRIBUTES = 0xFFFFFFFF
INVALID_HANDLE = ctypes.c_void_p(-1).value
# Ordinary file metadata. Cloud, device, offline, recall, and unknown flags are excluded.
ORDINARY_ATTRIBUTES = 0x00000001 | 0x00000002 | 0x00000004 | DIRECTORY | 0x00000020 | 0x00000080 | 0x00000100
ORDINARY_ATTRIBUTES |= 0x00000200 | 0x00000800 | 0x00002000 | 0x00004000 | 0x00008000 | 0x00020000

DWORD = ctypes.c_uint32
BOOL = ctypes.c_int32


class BasicInfo(ctypes.Structure):
    _fields_ = [
        ("creation", ctypes.c_int64), ("access", ctypes.c_int64),
        ("write", ctypes.c_int64), ("change", ctypes.c_int64), ("attributes", DWORD),
    ]


class StandardInfo(ctypes.Structure):
    _fields_ = [
        ("allocated", ctypes.c_int64), ("size", ctypes.c_int64), ("links", DWORD),
        ("delete_pending", ctypes.c_ubyte), ("directory", ctypes.c_ubyte),
    ]


class FileIdInfo(ctypes.Structure):
    _fields_ = [("volume", ctypes.c_uint64), ("identifier", ctypes.c_ubyte * 16)]


@lru_cache(maxsize=1)
def api():
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateFileW.argtypes = [ctypes.c_wchar_p, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, ctypes.c_void_p]
    dll.CreateFileW.restype = ctypes.c_void_p
    dll.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
    dll.GetFileAttributesW.restype = DWORD
    dll.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    dll.GetDriveTypeW.restype = DWORD
    dll.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, DWORD]
    dll.GetFileInformationByHandleEx.restype = BOOL
    dll.GetFileType.argtypes = [ctypes.c_void_p]
    dll.GetFileType.restype = DWORD
    dll.CloseHandle.argtypes = [ctypes.c_void_p]
    dll.CloseHandle.restype = BOOL
    return dll


def require_local_volume(anchor):
    if api().GetDriveTypeW(anchor) not in (2, 3, 6):
        raise ValueError("Select an ordinary local Windows volume.")


def check_attributes(attributes, directory=None):
    if attributes == INVALID_ATTRIBUTES:
        raise ctypes.WinError(ctypes.get_last_error())
    if attributes & ~ORDINARY_ATTRIBUTES:
        raise ValueError("The path has offline, reparse, recall, or unsupported Windows attributes.")
    is_directory = bool(attributes & DIRECTORY)
    if directory is not None and is_directory != directory:
        raise ValueError("The Windows path does not have the expected file type.")
    return is_directory


def handle_info(handle):
    dll = api()
    if dll.GetFileType(handle) != 1:
        raise ValueError("Use a regular file or directory on disk.")
    basic, standard, file_id = BasicInfo(), StandardInfo(), FileIdInfo()
    for information_class, buffer in ((0, basic), (1, standard), (18, file_id)):
        if not dll.GetFileInformationByHandleEx(handle, information_class, ctypes.byref(buffer), ctypes.sizeof(buffer)):
            raise ctypes.WinError(ctypes.get_last_error())
    directory = check_attributes(basic.attributes, bool(standard.directory))
    if standard.delete_pending:
        raise ValueError("The Windows path is pending deletion.")
    return SimpleNamespace(
        st_dev=file_id.volume, st_ino=int.from_bytes(file_id.identifier, "little"),
        st_mode=stat.S_IFDIR if directory else stat.S_IFREG, st_size=standard.size,
        st_nlink=standard.links, st_mtime_ns=basic.write * 100, st_ctime_ns=basic.change * 100,
        st_file_attributes=basic.attributes,
    )


@contextmanager
def open_handle(path, *, directory=None, read_bytes=False):
    dll = api()
    name = os.fspath(path)
    is_directory = check_attributes(dll.GetFileAttributesW(name), directory)
    access = GENERIC_READ if read_bytes else READ_ATTRIBUTES
    # Deny delete sharing on every ancestor, and deny write sharing on leaves.
    sharing = SHARE_READ | SHARE_WRITE if is_directory else SHARE_READ
    flags = OPEN_REPARSE_POINT | OPEN_NO_RECALL
    if is_directory:
        flags |= BACKUP_SEMANTICS
    handle = dll.CreateFileW(name, access, sharing, None, OPEN_EXISTING, flags, None)
    if handle == INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = handle_info(handle)
        check_attributes(info.st_file_attributes, is_directory)
        yield handle, info
    finally:
        if not dll.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())


def path_info(path):
    with open_handle(path) as (_, info):
        return info


@contextmanager
def file_stream(path):
    import msvcrt

    # Keep the original no-write/no-delete handle pinned while a second handle is read.
    with open_handle(path, directory=False, read_bytes=True) as (pinned, _):
        dll = api()
        name = os.fspath(path)
        flags = OPEN_REPARSE_POINT | OPEN_NO_RECALL
        handle = dll.CreateFileW(name, GENERIC_READ, SHARE_READ, None, OPEN_EXISTING, flags, None)
        if handle == INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        fd = None
        try:
            pinned_info = handle_info(pinned)
            opened = handle_info(handle)
            if (opened.st_dev, opened.st_ino) != (pinned_info.st_dev, pinned_info.st_ino):
                raise ValueError("The pinned Windows source was replaced.")
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            with os.fdopen(fd, "rb") as stream:
                yield stream
        finally:
            if fd is None and not dll.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())


def stream_info(stream):
    import msvcrt

    return handle_info(msvcrt.get_osfhandle(stream.fileno()))
