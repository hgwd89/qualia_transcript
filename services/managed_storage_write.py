from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from services.storage_paths import (
    _managed_root,
    _open_posix_directory_chain,
    _open_posix_directory_component,
    _open_windows_directory_chain,
    _validated_relative,
    _windows_close_handle,
    _windows_kernel32,
    _windows_open_path_handle,
    _windows_validate_handle_type,
)


@dataclass
class ManagedWriteFile:
    """New managed file bound to pinned ancestors until finish/abort."""

    stream: BinaryIO
    display_path: Path
    _posix_parent_fd: int | None = None
    _posix_name: str | None = None
    _windows_pinned: list[int] = field(default_factory=list)
    _released: bool = False

    def _release_pins(self) -> None:
        if self._released:
            return
        self._released = True
        if self._posix_parent_fd is not None:
            os.close(self._posix_parent_fd)
            self._posix_parent_fd = None
        for handle in reversed(self._windows_pinned):
            _windows_close_handle(handle)
        self._windows_pinned.clear()

    def finish(self) -> os.stat_result:
        """Flush/close the exact created file, then release ancestor pins."""
        try:
            self.stream.flush()
            info = os.fstat(self.stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"managed storage write target is not a regular file: {self.display_path}"
                )
            self.stream.close()
        except Exception:
            self.abort()
            raise
        self._release_pins()
        return info

    def abort(self) -> None:
        """Best-effort removal without allowing ancestor replacement to escape the root."""
        try:
            if os.name == "nt":
                try:
                    import msvcrt

                    handle = msvcrt.get_osfhandle(self.stream.fileno())
                    _windows_mark_delete_on_close(handle)
                except (OSError, ValueError):
                    pass
                try:
                    self.stream.close()
                except (OSError, ValueError):
                    pass
            else:
                if self._posix_parent_fd is not None and self._posix_name:
                    try:
                        os.unlink(self._posix_name, dir_fd=self._posix_parent_fd)
                    except (FileNotFoundError, OSError):
                        pass
                try:
                    self.stream.close()
                except (OSError, ValueError):
                    pass
        finally:
            self._release_pins()


def _supports_pinned_posix_write() -> bool:
    return (
        os.name != "nt"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in getattr(os, "supports_dir_fd", set())
    )


def _open_managed_file_for_write_posix(root: Path, relative: Path) -> ManagedWriteFile:
    parent_fd = _open_posix_directory_chain(root)
    display_parent = root
    try:
        for part in relative.parts[:-1]:
            display_parent = display_parent / part
            child_fd = _open_posix_directory_component(parent_fd, part, display_parent)
            os.close(parent_fd)
            parent_fd = child_fd

        name = relative.parts[-1]
        display_path = display_parent / name
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | int(getattr(os, "O_BINARY", 0) or 0)
        )
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(
                f"managed storage file could not be created safely: {display_path}"
            ) from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"managed storage write target is not a regular file: {display_path}"
                )
            stream = os.fdopen(fd, "w+b", closefd=True)
            fd = -1
            result = ManagedWriteFile(
                stream=stream,
                display_path=display_path,
                _posix_parent_fd=parent_fd,
                _posix_name=name,
            )
            parent_fd = -1
            return result
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)


def _windows_create_new_file(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    generic_read = 0x80000000
    generic_write = 0x40000000
    delete_access = 0x00010000
    file_share_read = 0x00000001
    create_new = 1
    file_attribute_normal = 0x00000080
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
        generic_read | generic_write | delete_access,
        file_share_read,
        None,
        create_new,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    invalid_handle_value = ctypes.c_void_p(-1).value
    if handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            "managed storage file could not be created safely",
            str(path),
        )
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
        raise OSError(error_code, "managed storage file could not be marked for deletion")


def _open_windows_delete_handle(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    file_read_attributes = 0x00000080
    delete_access = 0x00010000
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
        file_read_attributes | delete_access,
        file_share_read,
        None,
        open_existing,
        file_flag_open_reparse_point,
        None,
    )
    invalid_handle_value = ctypes.c_void_p(-1).value
    if handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        raise OSError(
            error_code,
            "managed storage file could not be opened for deletion",
            str(path),
        )
    return int(handle)


def _open_managed_file_for_write_windows(root: Path, relative: Path) -> ManagedWriteFile:
    import msvcrt

    pinned = _open_windows_directory_chain(root)
    current = root
    final_handle: int | None = None
    try:
        for part in relative.parts[:-1]:
            current = current / part
            child_handle = _windows_open_path_handle(current, directory=True)
            try:
                _windows_validate_handle_type(
                    child_handle,
                    directory=True,
                    display_path=current,
                )
            except Exception:
                _windows_close_handle(child_handle)
                raise
            pinned.append(child_handle)

        display_path = current / relative.parts[-1]
        final_handle = _windows_create_new_file(display_path)
        _windows_validate_handle_type(
            final_handle,
            directory=False,
            display_path=display_path,
        )

        flags = os.O_RDWR | int(getattr(os, "O_BINARY", 0) or 0)
        fd = msvcrt.open_osfhandle(final_handle, flags)
        final_handle = None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"managed storage write target is not a regular file: {display_path}"
                )
            stream = os.fdopen(fd, "w+b", closefd=True)
            fd = -1
            result = ManagedWriteFile(
                stream=stream,
                display_path=display_path,
                _windows_pinned=pinned,
            )
            pinned = []
            return result
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        raise ValueError("managed storage file could not be created safely") from exc
    finally:
        if final_handle is not None:
            _windows_close_handle(final_handle)
        for handle in reversed(pinned):
            _windows_close_handle(handle)


def open_managed_file_for_write(
    root_value: str | Path,
    stored_path: str,
) -> ManagedWriteFile:
    """Create a new regular managed file while pinning every ancestor."""
    root = _managed_root(root_value)
    relative = _validated_relative(stored_path)
    if not relative.parts:
        raise ValueError("managed storage path is invalid")

    if os.name == "nt":
        return _open_managed_file_for_write_windows(root, relative)
    if _supports_pinned_posix_write():
        return _open_managed_file_for_write_posix(root, relative)
    raise ValueError("platform cannot safely pin managed storage writes")


def unlink_managed_file(root_value: str | Path, stored_path: str) -> None:
    """Delete one managed regular file while pinning its ancestor chain."""
    root = _managed_root(root_value)
    relative = _validated_relative(stored_path)
    if not relative.parts:
        raise ValueError("managed storage path is invalid")

    if os.name == "nt":
        pinned = _open_windows_directory_chain(root)
        current = root
        final_handle: int | None = None
        try:
            for part in relative.parts[:-1]:
                current = current / part
                child_handle = _windows_open_path_handle(current, directory=True)
                try:
                    _windows_validate_handle_type(
                        child_handle,
                        directory=True,
                        display_path=current,
                    )
                except Exception:
                    _windows_close_handle(child_handle)
                    raise
                pinned.append(child_handle)
            display_path = current / relative.parts[-1]
            final_handle = _open_windows_delete_handle(display_path)
            _windows_validate_handle_type(
                final_handle,
                directory=False,
                display_path=display_path,
            )
            _windows_mark_delete_on_close(final_handle)
        finally:
            if final_handle is not None:
                _windows_close_handle(final_handle)
            for handle in reversed(pinned):
                _windows_close_handle(handle)
        return

    if not _supports_pinned_posix_write():
        raise ValueError("platform cannot safely pin managed storage deletion")

    parent_fd = _open_posix_directory_chain(root)
    try:
        display_parent = root
        for part in relative.parts[:-1]:
            display_parent = display_parent / part
            child_fd = _open_posix_directory_component(parent_fd, part, display_parent)
            os.close(parent_fd)
            parent_fd = child_fd
        name = relative.parts[-1]
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("managed storage deletion target is not a regular file")
        os.unlink(name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)
