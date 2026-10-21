"""Anchored, handle-relative file access for native Windows.

POSIX can open a file relative to a directory descriptor (``openat``) and
refuse to traverse a symlink with ``O_NOFOLLOW``.  Windows has neither.
The closest equivalent is ``NtCreateFile`` with
``OBJECT_ATTRIBUTES.RootDirectory`` set to a directory handle, combined with
``FILE_OPEN_REPARSE_POINT`` so the final component is opened as the reparse
point itself instead of its target.

The anchor only holds while the directory handle is open.  Two properties
make that true on Windows:

* the directory handle is opened without ``FILE_SHARE_DELETE``, and
* the child is opened relative to the handle rather than by path.

Without ``FILE_SHARE_DELETE`` the operating system refuses to rename or
delete the directory, and refuses to rename an ancestor of it, for as long
as the handle is open.  A path-resolved marker file cannot give that
guarantee, because the child open would follow whatever the path resolves
to at the moment of the open -- the classic time-of-check to time-of-use
window.

The primitives here are deliberately small.  They cover what the control
bus, the supervisor and the dashboard need: open a directory, open a file
relative to a directory, inspect the opened object, and truncate it.
"""

from __future__ import annotations

import ctypes
import msvcrt
import os
import sys
from ctypes import wintypes
from pathlib import Path

if sys.platform != "win32":  # pragma: no cover - module is Windows-only
    raise ImportError("windows_anchored is only available on native Windows")


# --- Win32 constants -------------------------------------------------------

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SYNCHRONIZE = 0x00100000
# GetFileInformationByHandle needs read-attribute access, which the generic
# read bit does not always imply for a handle opened without FILE_SHARE_WRITE.
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_APPEND_DATA = 0x0004

_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002

_OPEN_EXISTING = 3

_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010

_FILE_OPEN = 1
_FILE_CREATE = 2
_FILE_OPEN_IF = 3

_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_DIRECTORY_FILE = 0x00000001
_NT_FILE_OPEN_REPARSE_POINT = 0x00200000

_OBJ_CASE_INSENSITIVE = 0x00000040

_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", ctypes.c_wchar_p),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", ctypes.c_void_p),
        ("ObjectName", ctypes.POINTER(_UnicodeString)),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


class _IoStatusBlock(ctypes.Structure):
    _fields_ = [
        ("Status", ctypes.c_long),
        ("Information", ctypes.c_size_t),
    ]


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")

_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_ntdll.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
_ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG

_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
_kernel32.CreateFileW.restype = wintypes.HANDLE

_kernel32.GetFileInformationByHandle.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(_ByHandleFileInformation),
]
_kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

_kernel32.GetFinalPathNameByHandleW.argtypes = [
    wintypes.HANDLE,
    wintypes.LPWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
]
_kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD

_ntdll.NtCreateFile.argtypes = [
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.DWORD,
    ctypes.POINTER(_ObjectAttributes),
    ctypes.POINTER(_IoStatusBlock),
    ctypes.c_void_p,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    wintypes.ULONG,
    ctypes.c_void_p,
    wintypes.ULONG,
]
_ntdll.NtCreateFile.restype = ctypes.c_long


def _win_error(message: str) -> OSError:
    code = ctypes.get_last_error()
    if not code:
        code = ctypes.get_last_error()
    return OSError(code, f"{message}: {ctypes.FormatError(code) if code else 'unknown error'}")


def _nt_status_to_oserror(status: int, name: str) -> OSError:
    error = ctypes.WinError(_ntdll.RtlNtStatusToDosError(status))
    error.filename = name
    return error


def _describe_handle(handle: int) -> _ByHandleFileInformation:
    info = _ByHandleFileInformation()
    if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _win_error("GetFileInformationByHandle failed")
    return info


def _handle_to_fd(handle: int, flags: int) -> int:
    """Convert a Win32 handle into a CRT file descriptor.

    ``msvcrt.open_osfhandle`` transfers ownership of the handle to the CRT,
    so closing the descriptor closes the handle.

    The CRT flags are set explicitly.  ``open_osfhandle`` records the flags
    it is given, and a descriptor without ``O_APPEND`` would make
    ``os.fdopen(fd, "a")`` write at the current offset instead of the end
    of the file.
    """

    try:
        return msvcrt.open_osfhandle(handle, flags | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        _kernel32.CloseHandle(handle)
        raise


def _final_path(handle: int) -> Path:
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        written = _kernel32.GetFinalPathNameByHandleW(handle, buffer, size, 0)
        if written == 0:
            raise _win_error("GetFinalPathNameByHandleW failed")
        if written < size:
            text = buffer.value
            break
        size = written + 1
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return Path(text)


def _open_root(path: str | Path) -> int:
    """Open a directory and return a descriptor that anchors it.

    The handle is opened without ``FILE_SHARE_DELETE`` so the directory --
    and every ancestor of it -- cannot be renamed or replaced while the
    descriptor is open.  ``FILE_FLAG_OPEN_REPARSE_POINT`` means a junction
    or symlink at the final component is opened as itself, so the caller can
    detect and reject it.
    """

    absolute = Path(os.path.abspath(os.fspath(path)))
    # The handle is opened without FILE_SHARE_DELETE, so the directory and
    # every ancestor of it cannot be renamed or replaced while the handle
    # is open.  Only read-attribute access is requested: it is enough for
    # the metadata checks and it does not add a sharing restriction of its
    # own.
    handle = _kernel32.CreateFileW(
        str(absolute),
        _GENERIC_READ | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
        None,
    )
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise _win_error(f"cannot open directory: {absolute}")
    try:
        info = _describe_handle(handle)
        if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(f"control-bus path contains a reparse point: {absolute}")
        if not info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
            raise OSError(f"control-bus component is not a directory: {absolute}")
    except BaseException:
        _kernel32.CloseHandle(handle)
        raise
    return _handle_to_fd(handle, os.O_RDONLY)


def open_directory(path: str | Path, *, create: bool = False) -> int:
    """Walk each component relative to an opened directory, rejecting junctions.

    Keeping the parent open until its child is validated avoids checking a
    pathname and then following a replacement while opening that pathname.
    """
    absolute = Path(os.path.abspath(os.fspath(path)))
    current = _open_root(absolute.anchor)
    try:
        for name in absolute.parts[1:]:
            child = open_relative(
                current, name, access=_GENERIC_READ, create=create,
                allow_directory=True, directory_only=True,
            )
            try:
                if is_reparse_point(child) or not is_directory(child):
                    raise OSError(f"unsafe directory component: {name}")
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def open_relative(
    dir_fd: int,
    name: str,
    *,
    access: int,
    create: bool = False,
    exclusive: bool = False,
    allow_directory: bool = False,
    directory_only: bool = False,
    append: bool = False,
) -> int:
    """Open ``name`` relative to an anchored directory descriptor.

    This is the Windows counterpart of ``openat(dir_fd, name, ...)``.  The
    name is resolved against the directory object the descriptor refers to,
    never against a path, so a renamed or replaced ancestor cannot redirect
    the open.  ``FILE_OPEN_REPARSE_POINT`` keeps the final component
    unfollowed, so a symlink or junction is returned as itself and the
    caller can reject it.
    """

    if not name or name in {".", ".."} or any(c in name for c in ("/", "\\", ":", "\0")):
        raise OSError("unsafe file name")

    if create and exclusive:
        disposition = _FILE_CREATE
    elif create:
        disposition = _FILE_OPEN_IF
    else:
        disposition = _FILE_OPEN

    # O_APPEND has to be requested at open time: the write must go to the
    # current end of the file.  Opening without it and seeking afterwards
    # would let a concurrent append interleave.  The append data access is
    # the actual guarantee; the CRT flag below only keeps the descriptor's
    # own notion of the position in step with it.
    if append:
        access |= _FILE_APPEND_DATA

    name_bytes = name.encode("utf-16-le")
    if len(name_bytes) > 65532:
        raise OSError("file name is too long")
    buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UnicodeString(
        len(name_bytes),
        len(name_bytes) + 2,
        ctypes.cast(buffer, ctypes.c_wchar_p),
    )
    attributes = _ObjectAttributes()
    attributes.Length = ctypes.sizeof(_ObjectAttributes)
    attributes.RootDirectory = msvcrt.get_osfhandle(dir_fd)
    attributes.ObjectName = ctypes.pointer(unicode_name)
    attributes.Attributes = _OBJ_CASE_INSENSITIVE

    status_block = _IoStatusBlock()
    handle = ctypes.c_void_p()
    status = _ntdll.NtCreateFile(
        ctypes.byref(handle),
        access | _SYNCHRONIZE | _FILE_READ_ATTRIBUTES | _GENERIC_READ,
        ctypes.byref(attributes),
        ctypes.byref(status_block),
        None,
        0x00000080,  # FILE_ATTRIBUTE_NORMAL
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        disposition,
        (
            _FILE_SYNCHRONOUS_IO_NONALERT
            | _NT_FILE_OPEN_REPARSE_POINT
            | (_FILE_DIRECTORY_FILE if directory_only else 0 if allow_directory else _FILE_NON_DIRECTORY_FILE)
        ),
        None,
        0,
    )
    if status < 0:
        raise _nt_status_to_oserror(status, name)
    if not handle.value:
        raise _win_error(f"cannot open relative file: {name}")
    crt_flags = os.O_RDWR if access & _GENERIC_WRITE else os.O_RDONLY
    if append:
        crt_flags |= os.O_APPEND
    return _handle_to_fd(handle.value, crt_flags)


def file_attributes(fd: int) -> int:
    """Return the Win32 file attributes of an open descriptor."""

    return _describe_handle(msvcrt.get_osfhandle(fd)).dwFileAttributes


def link_count(fd: int) -> int:
    """Return the number of hard links to the open file."""

    return int(_describe_handle(msvcrt.get_osfhandle(fd)).nNumberOfLinks)


def is_reparse_point(fd: int) -> bool:
    """Report whether the open object is itself a reparse point."""

    return bool(file_attributes(fd) & _FILE_ATTRIBUTE_REPARSE_POINT)


def is_directory(fd: int) -> bool:
    """Report whether the open object is a directory."""

    return bool(file_attributes(fd) & _FILE_ATTRIBUTE_DIRECTORY)


def final_path(fd: int) -> Path:
    """Return the current path of an open descriptor.

    For a directory this is the directory's own path.  Because the handle
    is opened without ``FILE_SHARE_DELETE`` the directory cannot be renamed
    while it is open, so the path cannot change underneath the caller.
    """

    return _final_path(msvcrt.get_osfhandle(fd))


def truncate(fd: int) -> None:
    """Truncate an open file to zero length.

    The truncation is applied to the already-open object, so it cannot
    affect a different file than the one that was validated.
    """

    os.ftruncate(fd, 0)
