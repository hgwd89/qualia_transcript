from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


@dataclass
class ManagedReadFile:
    stream: BinaryIO
    stat_result: os.stat_result

    def close(self) -> None:
        self.stream.close()


@dataclass
class ManagedWriteFile:
    stream: BinaryIO
    stat_result: os.stat_result

    def close(self) -> None:
        self.stream.close()


def is_link_or_reparse(path: Path) -> bool:
    """Return True for symlinks and Windows reparse-point entries/junctions."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _managed_root(root_value: str | Path) -> Path:
    try:
        return Path(root_value).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("managed storage root is invalid") from exc


def _validated_relative(stored_path: str) -> Path:
    normalized = str(stored_path or "").replace("\\", "/")
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("managed storage path is invalid")
    return relative


def _assert_unlinked_components(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts:
        current = current / part
        if is_link_or_reparse(current):
            raise ValueError("managed storage path contains a linked/reparse entry")


def _directory_identity(info: os.stat_result) -> tuple[int, int, int]:
    return int(info.st_dev), int(info.st_ino), int(info.st_mode)


def _file_generation(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def resolve_managed_path(root_value: str | Path, stored_path: str) -> Path:
    """Resolve one managed path without accepting linked/reparse components."""
    root = _managed_root(root_value)
    relative = _validated_relative(stored_path)
    lexical = root / relative

    _assert_unlinked_components(root, relative)

    try:
        candidate = lexical.resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("managed storage path escapes root") from exc
    return candidate


def _supports_pinned_posix_read() -> bool:
    return (
        os.name != "nt"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_dir_fd", set())
    )


def _open_posix_directory_component(
    parent_fd: int,
    name: str,
    display_path: Path,
) -> int:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"managed storage directory is unreadable: {display_path}") from exc
    if not stat.S_ISDIR(before.st_mode):
        raise ValueError(f"managed storage path contains a non-directory component: {display_path}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        child_fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ValueError(f"managed storage directory could not be opened safely: {display_path}") from exc
    try:
        opened = os.fstat(child_fd)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _directory_identity(before) != _directory_identity(opened)
            or _directory_identity(after) != _directory_identity(opened)
        ):
            raise ValueError(f"managed storage directory changed during open: {display_path}")
        return child_fd
    except Exception:
        os.close(child_fd)
        raise


def _open_posix_directory_chain(path: Path) -> int:
    if not path.is_absolute():
        raise ValueError("managed storage root must be absolute")
    anchor = Path(path.anchor)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current_fd = os.open(anchor, flags)
    except OSError as exc:
        raise ValueError(f"managed storage root is unreadable: {path}") from exc

    current_path = anchor
    try:
        for part in path.parts[1:]:
            next_path = current_path / part
            child_fd = _open_posix_directory_component(current_fd, part, next_path)
            os.close(current_fd)
            current_fd = child_fd
            current_path = next_path
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_managed_file_posix(root: Path, relative: Path) -> ManagedReadFile:
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
        try:
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"managed storage file is unreadable: {display_path}") from exc
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"managed storage path is not a regular file: {display_path}")

        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0) | os.O_NOFOLLOW
        try:
            fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"managed storage file could not be opened safely: {display_path}") from exc
        try:
            opened = os.fstat(fd)
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _file_generation(before) != _file_generation(opened)
                or _file_generation(after) != _file_generation(opened)
            ):
                raise ValueError(f"managed storage file changed during open: {display_path}")
            stream = os.fdopen(fd, "rb", closefd=True)
            fd = -1
            return ManagedReadFile(stream=stream, stat_result=opened)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(parent_fd)


def _create_managed_file_posix(root: Path, relative: Path) -> ManagedWriteFile:
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
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | int(getattr(os, "O_BINARY", 0) or 0)
        )
        try:
            fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"managed storage file could not be created safely: {display_path}") from exc
        try:
            opened = os.fstat(fd)
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _file_generation(after) != _file_generation(opened)
            ):
                raise ValueError(f"managed storage file changed during create: {display_path}")
            stream = os.fdopen(fd, "wb", closefd=True)
            fd = -1
            return ManagedWriteFile(stream=stream, stat_result=opened)
        finally:
            if fd >= 0:
                os.close(fd)
    finally:
        os.close(parent_fd)


def _windows_kernel32():
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)


def _windows_close_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = _windows_kernel32().CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _windows_open_path_handle(path: Path, *, directory: bool, read_data: bool = False) -> int:
    import ctypes
    from ctypes import wintypes

    file_read_attributes = 0x00000080
    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000

    desired_access = generic_read if read_data else file_read_attributes
    flags = file_flag_open_reparse_point
    if directory:
        flags |= file_flag_backup_semantics

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

    # Permit only concurrent readers. Omitting both FILE_SHARE_WRITE and
    # FILE_SHARE_DELETE prevents reparse metadata mutation, rename, deletion, or
    # replacement while a managed component remains pinned by this handle.
    handle = create_file(
        str(path),
        desired_access,
        file_share_read,
        None,
        open_existing,
        flags,
        None,
    )
    invalid_handle_value = ctypes.c_void_p(-1).value
    if handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "managed storage path could not be opened safely", str(path))
    return int(handle)


def _windows_create_file_handle(path: Path, *, write_through: bool = False) -> int:
    import ctypes
    from ctypes import wintypes

    generic_write = 0x40000000
    file_read_attributes = 0x00000080
    file_share_read = 0x00000001
    create_new = 1
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    file_flag_write_through = 0x80000000

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

    create_flags = file_attribute_normal | file_flag_open_reparse_point
    if write_through:
        create_flags |= file_flag_write_through

    handle = create_file(
        str(path),
        generic_write | file_read_attributes,
        file_share_read,
        None,
        create_new,
        create_flags,
        None,
    )
    invalid_handle_value = ctypes.c_void_p(-1).value
    if handle == invalid_handle_value:
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "managed storage file could not be created safely", str(path))
    return int(handle)


def _windows_handle_attributes(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
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

    get_info = _windows_kernel32().GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_info.restype = wintypes.BOOL
    info = ByHandleFileInformation()
    if not get_info(handle, ctypes.byref(info)):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "could not inspect managed storage handle")
    return int(info.dwFileAttributes)


def _windows_validate_handle_type(handle: int, *, directory: bool, display_path: Path) -> None:
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    attributes = _windows_handle_attributes(handle)
    if attributes & file_attribute_reparse_point:
        raise ValueError(f"managed storage path contains a linked/reparse entry: {display_path}")
    is_directory = bool(attributes & file_attribute_directory)
    if directory != is_directory:
        expected = "directory" if directory else "regular file"
        raise ValueError(f"managed storage path is not a {expected}: {display_path}")


def _open_windows_directory_chain(path: Path) -> list[int]:
    if not path.is_absolute():
        raise ValueError("managed storage root must be absolute")

    handles: list[int] = []
    current = Path(path.anchor)
    try:
        anchor_handle = _windows_open_path_handle(current, directory=True)
        _windows_validate_handle_type(anchor_handle, directory=True, display_path=current)
        handles.append(anchor_handle)

        for part in path.parts[1:]:
            current = current / part
            child_handle = _windows_open_path_handle(current, directory=True)
            try:
                _windows_validate_handle_type(child_handle, directory=True, display_path=current)
            except Exception:
                _windows_close_handle(child_handle)
                raise
            handles.append(child_handle)
        return handles
    except Exception:
        for handle in reversed(handles):
            _windows_close_handle(handle)
        raise


def _open_managed_file_windows(root: Path, relative: Path) -> ManagedReadFile:
    import msvcrt

    pinned = _open_windows_directory_chain(root)
    current = root
    final_handle: int | None = None
    try:
        for part in relative.parts[:-1]:
            current = current / part
            child_handle = _windows_open_path_handle(current, directory=True)
            try:
                _windows_validate_handle_type(child_handle, directory=True, display_path=current)
            except Exception:
                _windows_close_handle(child_handle)
                raise
            pinned.append(child_handle)

        display_path = current / relative.parts[-1]
        final_handle = _windows_open_path_handle(
            display_path,
            directory=False,
            read_data=True,
        )
        _windows_validate_handle_type(final_handle, directory=False, display_path=display_path)

        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
        fd = msvcrt.open_osfhandle(final_handle, flags)
        final_handle = None  # ownership transferred to the CRT file descriptor
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"managed storage path is not a regular file: {display_path}")
            stream = os.fdopen(fd, "rb", closefd=True)
            fd = -1
            return ManagedReadFile(stream=stream, stat_result=opened)
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        raise ValueError("managed storage file could not be opened safely") from exc
    finally:
        if final_handle is not None:
            _windows_close_handle(final_handle)
        for handle in reversed(pinned):
            _windows_close_handle(handle)


def _create_managed_file_windows(
    root: Path,
    relative: Path,
    *,
    write_through: bool = False,
) -> ManagedWriteFile:
    import msvcrt

    pinned = _open_windows_directory_chain(root)
    current = root
    final_handle: int | None = None
    try:
        for part in relative.parts[:-1]:
            current = current / part
            child_handle = _windows_open_path_handle(current, directory=True)
            try:
                _windows_validate_handle_type(child_handle, directory=True, display_path=current)
            except Exception:
                _windows_close_handle(child_handle)
                raise
            pinned.append(child_handle)

        display_path = current / relative.parts[-1]
        final_handle = _windows_create_file_handle(
            display_path,
            write_through=write_through,
        )
        _windows_validate_handle_type(final_handle, directory=False, display_path=display_path)

        flags = os.O_WRONLY | int(getattr(os, "O_BINARY", 0) or 0)
        fd = msvcrt.open_osfhandle(final_handle, flags)
        final_handle = None  # ownership transferred to the CRT file descriptor
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError(f"managed storage path is not a regular file: {display_path}")
            stream = os.fdopen(fd, "wb", closefd=True)
            fd = -1
            return ManagedWriteFile(stream=stream, stat_result=opened)
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


def open_managed_file_for_read(root_value: str | Path, stored_path: str) -> ManagedReadFile:
    """Open a managed regular file while pinning every ancestor during acquisition.

    The returned stream is bound to the exact file object opened beneath the resolved
    managed root. POSIX walks with descriptor-relative ``open/stat`` and
    ``O_NOFOLLOW``. Windows retains read-share-only directory handles opened with
    ``FILE_FLAG_OPEN_REPARSE_POINT`` while descending, preventing rename/replacement
    and write-side reparse mutation before final-file acquisition.
    """
    root = _managed_root(root_value)
    relative = _validated_relative(stored_path)
    if not relative.parts:
        raise ValueError("managed storage path is invalid")

    if os.name == "nt":
        return _open_managed_file_windows(root, relative)
    if _supports_pinned_posix_read():
        return _open_managed_file_posix(root, relative)
    raise ValueError("platform cannot safely pin managed storage reads")


def open_managed_file_for_create(
    root_value: str | Path,
    stored_path: str,
    *,
    write_through: bool = False,
) -> ManagedWriteFile:
    """Create a new managed regular file through an ancestry-pinned boundary.

    The destination basename is created exclusively, so an existing entry is never
    truncated or followed. POSIX creates descriptor-relative beneath a pinned parent
    with ``O_EXCL|O_NOFOLLOW``. Windows retains read-share-only ancestor handles and
    uses ``CREATE_NEW`` with ``FILE_FLAG_OPEN_REPARSE_POINT`` for the final file.
    The returned stream remains bound to the created file object after acquisition.
    """
    root = _managed_root(root_value)
    relative = _validated_relative(stored_path)
    if not relative.parts:
        raise ValueError("managed storage path is invalid")

    if os.name == "nt":
        return _create_managed_file_windows(
            root,
            relative,
            write_through=write_through,
        )
    if _supports_pinned_posix_read():
        return _create_managed_file_posix(root, relative)
    raise ValueError("platform cannot safely pin managed storage writes")


def ensure_managed_id_dir(root_value: str | Path, value: int) -> Path:
    """Create/return root/<positive integer ID> only when it is not linked."""
    value = int(value)
    if value <= 0:
        raise ValueError("managed storage id must be positive")

    root = _managed_root(root_value)
    path = root / str(value)
    if is_link_or_reparse(path):
        raise ValueError("managed ID directory is linked/reparse")

    path.mkdir(parents=True, exist_ok=True)

    if is_link_or_reparse(path):
        raise ValueError("managed ID directory is linked/reparse")
    if not path.is_dir():
        raise ValueError("managed ID path is not a directory")
    try:
        if path.resolve() != path:
            raise ValueError("managed ID directory resolves elsewhere")
    except (OSError, RuntimeError) as exc:
        raise ValueError("managed ID directory is invalid") from exc
    return path
