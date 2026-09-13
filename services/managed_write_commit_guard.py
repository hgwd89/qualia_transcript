from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from services.storage_paths import (
    _file_generation,
    _managed_root,
    _open_posix_directory_chain,
    _open_posix_directory_component,
    _open_windows_directory_chain,
    _validated_relative,
    _windows_close_handle,
    _windows_kernel32,
    _windows_open_path_handle,
    _windows_validate_handle_type,
    resolve_managed_path,
)


@dataclass
class ManagedWriteCommitGuard:
    """Pin one already-written managed file through metadata commit/cleanup.

    The initial managed writer closes before this guard is acquired. Acquisition
    therefore reopens the *current* exact file generation and rejects any
    replacement that already happened. Once acquired, the parent ancestry and file
    object remain pinned until DB commit and post-commit namespace verification are
    complete. Rollback cleanup operates through that pinned parent/file rather than
    resolving the mutable managed pathname again.
    """

    root: Path
    relative: Path
    expected: os.stat_result
    _posix_parent_fd: int | None = None
    _posix_file_fd: int | None = None
    _windows_file_fd: int | None = None
    _windows_pinned: list[int] = field(default_factory=list)
    _closed: bool = False

    @property
    def _name(self) -> str:
        return self.relative.parts[-1]

    def verify_pinned_file(self) -> None:
        fd = self._posix_file_fd
        if fd is None:
            fd = self._windows_file_fd
        if fd is None:
            raise ValueError("managed write commit guard is closed")
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode):
            raise ValueError("managed write commit target is not a regular file")
        if _file_generation(current) != _file_generation(self.expected):
            raise ValueError("managed write commit target changed after write")

    def verify_namespace(self) -> None:
        """Require the public managed pathname to still name the pinned generation."""
        self.verify_pinned_file()
        try:
            current_path = resolve_managed_path(self.root, str(self.relative))
            current = current_path.stat()
        except (OSError, ValueError) as exc:
            raise ValueError("managed write namespace changed after write") from exc
        if _file_generation(current) != _file_generation(self.expected):
            raise ValueError("managed write namespace changed after write")

    def discard(self) -> bool:
        """Delete only the pinned generation; never follow a replacement ancestry."""
        if self._closed:
            return False
        self.verify_pinned_file()

        if os.name == "nt":
            if self._windows_file_fd is None:
                return False
            handle = _windows_os_handle(self._windows_file_fd)
            _windows_mark_delete_on_close(handle)
            os.close(self._windows_file_fd)
            self._windows_file_fd = None
            return True

        if self._posix_parent_fd is None:
            return False
        try:
            current = os.stat(
                self._name,
                dir_fd=self._posix_parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if _file_generation(current) != _file_generation(self.expected):
            # A different basename successor appeared inside the pinned parent.
            # Leave it untouched rather than risking data loss.
            return False
        os.unlink(self._name, dir_fd=self._posix_parent_fd)
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._posix_file_fd is not None:
            os.close(self._posix_file_fd)
            self._posix_file_fd = None
        if self._posix_parent_fd is not None:
            os.close(self._posix_parent_fd)
            self._posix_parent_fd = None
        if self._windows_file_fd is not None:
            os.close(self._windows_file_fd)
            self._windows_file_fd = None
        for handle in reversed(self._windows_pinned):
            _windows_close_handle(handle)
        self._windows_pinned.clear()

    def __enter__(self) -> "ManagedWriteCommitGuard":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _open_posix_guard(root: Path, relative: Path, expected: os.stat_result) -> ManagedWriteCommitGuard:
    parent_fd = _open_posix_directory_chain(root)
    file_fd: int | None = None
    display_parent = root
    try:
        for part in relative.parts[:-1]:
            display_parent = display_parent / part
            child_fd = _open_posix_directory_component(parent_fd, part, display_parent)
            os.close(parent_fd)
            parent_fd = child_fd

        name = relative.parts[-1]
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("managed write commit target is unavailable") from exc
        if _file_generation(current) != _file_generation(expected):
            raise ValueError("managed write commit target changed after write")

        flags = os.O_RDONLY | os.O_NOFOLLOW | int(getattr(os, "O_BINARY", 0) or 0)
        try:
            file_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError("managed write commit target could not be pinned") from exc
        opened = os.fstat(file_fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _file_generation(opened) != _file_generation(expected)
            or _file_generation(after) != _file_generation(expected)
        ):
            raise ValueError("managed write commit target changed while pinning")

        guard = ManagedWriteCommitGuard(
            root=root,
            relative=relative,
            expected=expected,
            _posix_parent_fd=parent_fd,
            _posix_file_fd=file_fd,
        )
        parent_fd = -1
        file_fd = None
        guard.verify_namespace()
        return guard
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _windows_os_handle(fd: int) -> int:
    import msvcrt

    return int(msvcrt.get_osfhandle(fd))


def _windows_open_commit_file(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    generic_read = 0x80000000
    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    file_share_read = 0x00000001
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000

    create_file = _windows_kernel32().CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        generic_read | delete_access | file_read_attributes,
        file_share_read,
        None,
        open_existing,
        file_flag_open_reparse_point,
        None,
    )
    invalid_handle_value = ctypes.c_void_p(-1).value
    if handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "managed write commit file could not be opened", str(path))
    return int(handle)


def _windows_mark_delete_on_close(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    file_disposition_info = 4

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_ubyte)]

    set_info = _windows_kernel32().SetFileInformationByHandle
    set_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_info.restype = wintypes.BOOL
    info = FileDispositionInfo(1)
    if not set_info(
        handle,
        file_disposition_info,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "managed write commit file could not be deleted safely")


def _open_windows_guard(root: Path, relative: Path, expected: os.stat_result) -> ManagedWriteCommitGuard:
    import msvcrt

    pinned = _open_windows_directory_chain(root)
    current = root
    final_handle: int | None = None
    file_fd: int | None = None
    try:
        for part in relative.parts[:-1]:
            current = current / part
            child = _windows_open_path_handle(current, directory=True)
            try:
                _windows_validate_handle_type(child, directory=True, display_path=current)
            except Exception:
                _windows_close_handle(child)
                raise
            pinned.append(child)

        display_path = current / relative.parts[-1]
        final_handle = _windows_open_commit_file(display_path)
        _windows_validate_handle_type(final_handle, directory=False, display_path=display_path)

        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
        file_fd = msvcrt.open_osfhandle(final_handle, flags)
        final_handle = None
        opened = os.fstat(file_fd)
        if _file_generation(opened) != _file_generation(expected):
            raise ValueError("managed write commit target changed after write")

        guard = ManagedWriteCommitGuard(
            root=root,
            relative=relative,
            expected=expected,
            _windows_file_fd=file_fd,
            _windows_pinned=pinned,
        )
        file_fd = None
        pinned = []
        guard.verify_namespace()
        return guard
    except OSError as exc:
        raise ValueError("managed write commit target could not be pinned") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if final_handle is not None:
            _windows_close_handle(final_handle)
        for handle in reversed(pinned):
            _windows_close_handle(handle)


def open_managed_write_commit_guard(
    root_value: str | Path,
    stored_path: str,
    expected: os.stat_result,
) -> ManagedWriteCommitGuard:
    """Re-pin the exact written generation for the full metadata commit window."""
    root = _managed_root(root_value)
    relative = _validated_relative(stored_path)
    if not relative.parts:
        raise ValueError("managed storage path is invalid")

    if os.name == "nt":
        return _open_windows_guard(root, relative, expected)
    return _open_posix_guard(root, relative, expected)
