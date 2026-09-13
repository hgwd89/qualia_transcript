"""Hardened storage boundary for immutable raw transcript JSON evidence."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from services.storage_paths import (
    _directory_identity,
    _file_generation,
    _open_posix_directory_chain,
    _open_posix_directory_component,
    _open_windows_directory_chain,
    _supports_pinned_posix_read,
    _windows_close_handle,
    _windows_open_path_handle,
    _windows_validate_handle_type,
    is_link_or_reparse,
    open_managed_file_for_create,
    open_managed_file_for_read,
)


RAW_SNAPSHOT_DIR = "raw_transcripts"


def _root(output_dir: str | Path) -> Path:
    try:
        return Path(output_dir).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("raw snapshot output root is invalid") from exc


def _readable_root(output_dir: str | Path) -> Path | None:
    """Resolve a read root while preserving the legacy empty-missing-root behavior."""
    root = _root(output_dir)
    if not root.exists():
        return None
    if not root.is_dir():
        raise ValueError("raw snapshot output root is not a directory")
    return root


def _validate_base_name(base_name: str) -> str:
    value = str(base_name or "").strip()
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise ValueError("raw snapshot basename is invalid")
    if value.lower().endswith(".json"):
        value = value[:-5]
    if not value:
        raise ValueError("raw snapshot basename is invalid")
    return value


def ensure_raw_snapshot_dir(output_dir: str | Path) -> Path:
    """Create/return the canonical raw snapshot directory without accepting links."""
    root = _root(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    raw_dir = root / RAW_SNAPSHOT_DIR
    if is_link_or_reparse(raw_dir):
        raise ValueError("raw snapshot directory is linked/reparse")
    raw_dir.mkdir(exist_ok=True)
    if is_link_or_reparse(raw_dir):
        raise ValueError("raw snapshot directory is linked/reparse")
    if not raw_dir.is_dir():
        raise ValueError("raw snapshot path is not a directory")
    try:
        if raw_dir.resolve() != raw_dir:
            raise ValueError("raw snapshot directory resolves elsewhere")
    except (OSError, RuntimeError) as exc:
        raise ValueError("raw snapshot directory is invalid") from exc
    return raw_dir


def _open_raw_dir_posix(root: Path) -> tuple[int, int] | None:
    if not _supports_pinned_posix_read():
        raise ValueError("platform cannot safely pin raw snapshot directory listing")

    root_fd = _open_posix_directory_chain(root)
    try:
        try:
            info = os.stat(RAW_SNAPSHOT_DIR, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.close(root_fd)
            return None
        except OSError as exc:
            raise ValueError("raw snapshot directory is unreadable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("raw snapshot path is not a directory")

        raw_fd = _open_posix_directory_component(
            root_fd,
            RAW_SNAPSHOT_DIR,
            root / RAW_SNAPSHOT_DIR,
        )
        return root_fd, raw_fd
    except Exception:
        os.close(root_fd)
        raise


def _windows_handle_identity(handle: int) -> tuple[int, int]:
    """Return stable volume/file-index identity for an already-open Windows handle."""
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

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_info.restype = wintypes.BOOL
    info = ByHandleFileInformation()
    if not get_info(handle, ctypes.byref(info)):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, "could not inspect raw snapshot directory handle")
    file_index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
    return int(info.dwVolumeSerialNumber), file_index


def _pin_raw_dir_windows(root: Path) -> tuple[list[int], Path, tuple[int, int]] | None:
    pinned = _open_windows_directory_chain(root)
    raw_handle: int | None = None
    raw_dir = root / RAW_SNAPSHOT_DIR
    try:
        try:
            raw_handle = _windows_open_path_handle(raw_dir, directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) in {2, 3} or getattr(exc, "errno", None) in {2, 3}:
                for handle in reversed(pinned):
                    _windows_close_handle(handle)
                return None
            raise ValueError("raw snapshot directory could not be opened safely") from exc
        _windows_validate_handle_type(raw_handle, directory=True, display_path=raw_dir)
        identity = _windows_handle_identity(raw_handle)
        pinned.append(raw_handle)
        raw_handle = None
        return pinned, raw_dir, identity
    except Exception:
        if raw_handle is not None:
            _windows_close_handle(raw_handle)
        for handle in reversed(pinned):
            _windows_close_handle(handle)
        raise


def _assert_windows_raw_path_identity(raw_dir: Path, expected: tuple[int, int]) -> None:
    """Fail closed if the public raw-directory pathname no longer names the pinned object."""
    handle: int | None = None
    try:
        handle = _windows_open_path_handle(raw_dir, directory=True)
        _windows_validate_handle_type(handle, directory=True, display_path=raw_dir)
        if _windows_handle_identity(handle) != expected:
            raise ValueError("raw snapshot directory changed during batch read")
    except OSError as exc:
        raise ValueError("raw snapshot directory changed during batch read") from exc
    finally:
        if handle is not None:
            _windows_close_handle(handle)


def _json_names(names) -> list[str]:
    return sorted(str(name) for name in names if str(name).lower().endswith(".json"))


def _read_raw_snapshot_batch_posix(root: Path) -> list[tuple[str, bytes]]:
    opened = _open_raw_dir_posix(root)
    if opened is None:
        return []
    root_fd, raw_fd = opened
    try:
        try:
            names = _json_names(os.listdir(raw_fd))
        except (OSError, TypeError) as exc:
            raise ValueError("raw snapshot directory could not be listed safely") from exc

        result: list[tuple[str, bytes]] = []
        for name in names:
            display_path = root / RAW_SNAPSHOT_DIR / name
            try:
                before = os.stat(name, dir_fd=raw_fd, follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"raw snapshot is unreadable: {display_path}") from exc
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"raw snapshot is not a regular file: {display_path}")

            flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0) | os.O_NOFOLLOW
            try:
                fd = os.open(name, flags, dir_fd=raw_fd)
            except OSError as exc:
                raise ValueError(f"raw snapshot could not be opened safely: {display_path}") from exc
            try:
                opened_stat = os.fstat(fd)
                after_open = os.stat(name, dir_fd=raw_fd, follow_symlinks=False)
                opened_generation = _file_generation(opened_stat)
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or _file_generation(before) != opened_generation
                    or _file_generation(after_open) != opened_generation
                ):
                    raise ValueError(f"raw snapshot changed during open: {display_path}")
                with os.fdopen(fd, "rb", closefd=True) as stream:
                    fd = -1
                    snapshot_bytes = stream.read()
                    after_read = os.fstat(stream.fileno())
                    try:
                        path_after_read = os.stat(
                            name,
                            dir_fd=raw_fd,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise ValueError(
                            f"raw snapshot changed during read: {display_path}"
                        ) from exc
                    if (
                        _file_generation(after_read) != opened_generation
                        or _file_generation(path_after_read) != opened_generation
                    ):
                        raise ValueError(f"raw snapshot changed during read: {display_path}")
                    result.append((name, snapshot_bytes))
            finally:
                if fd >= 0:
                    os.close(fd)
        return result
    finally:
        os.close(raw_fd)
        os.close(root_fd)


def _read_raw_snapshot_batch_windows(root: Path) -> list[tuple[str, bytes]]:
    import msvcrt

    opened = _pin_raw_dir_windows(root)
    if opened is None:
        return []
    pinned, raw_dir, identity = opened
    try:
        try:
            names = _json_names(os.listdir(raw_dir))
        except OSError as exc:
            raise ValueError("raw snapshot directory could not be listed safely") from exc
        _assert_windows_raw_path_identity(raw_dir, identity)

        result: list[tuple[str, bytes]] = []
        for name in names:
            display_path = raw_dir / name
            final_handle: int | None = None
            fd = -1
            try:
                final_handle = _windows_open_path_handle(
                    display_path,
                    directory=False,
                    read_data=True,
                )
                _windows_validate_handle_type(
                    final_handle,
                    directory=False,
                    display_path=display_path,
                )
                flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
                fd = msvcrt.open_osfhandle(final_handle, flags)
                final_handle = None
                opened_stat = os.fstat(fd)
                if not stat.S_ISREG(opened_stat.st_mode):
                    raise ValueError(f"raw snapshot is not a regular file: {display_path}")

                _assert_windows_raw_path_identity(raw_dir, identity)
                try:
                    current_stat = os.stat(display_path, follow_symlinks=False)
                except OSError as exc:
                    raise ValueError(f"raw snapshot changed during open: {display_path}") from exc
                if _file_generation(current_stat) != _file_generation(opened_stat):
                    raise ValueError(f"raw snapshot changed during open: {display_path}")

                with os.fdopen(fd, "rb", closefd=True) as stream:
                    fd = -1
                    result.append((name, stream.read()))
            except OSError as exc:
                raise ValueError(f"raw snapshot could not be opened safely: {display_path}") from exc
            finally:
                if fd >= 0:
                    os.close(fd)
                if final_handle is not None:
                    _windows_close_handle(final_handle)
        return result
    finally:
        for handle in reversed(pinned):
            _windows_close_handle(handle)


def read_raw_snapshot_batch(output_dir: str | Path) -> list[tuple[str, bytes]]:
    """Read all JSON evidence while retaining one verified raw-directory identity."""
    root = _readable_root(output_dir)
    if root is None:
        return []
    if os.name == "nt":
        return _read_raw_snapshot_batch_windows(root)
    return _read_raw_snapshot_batch_posix(root)


def _stable_written_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
    )


def _sync_raw_snapshot_parent_posix(
    root: Path,
    filename: str,
    expected_file_stat: os.stat_result,
) -> None:
    opened = _open_raw_dir_posix(root)
    if opened is None:
        raise ValueError("raw snapshot directory disappeared before durability sync")
    root_fd, raw_fd = opened
    expected_generation = _file_generation(expected_file_stat)
    try:
        try:
            current = os.stat(filename, dir_fd=raw_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("raw snapshot disappeared before durability sync") from exc
        if not stat.S_ISREG(current.st_mode) or _file_generation(current) != expected_generation:
            raise ValueError("raw snapshot changed before durability sync")

        pinned_raw_identity = _directory_identity(os.fstat(raw_fd))
        os.fsync(raw_fd)
        # raw_transcripts itself may have been created for this first snapshot.
        # Flush the managed root as well so that directory entry is durable.
        os.fsync(root_fd)

        after_sync = os.stat(filename, dir_fd=raw_fd, follow_symlinks=False)
        public_raw = os.stat(RAW_SNAPSHOT_DIR, dir_fd=root_fd, follow_symlinks=False)
        if (
            _file_generation(after_sync) != expected_generation
            or not stat.S_ISDIR(public_raw.st_mode)
            or _directory_identity(public_raw) != pinned_raw_identity
        ):
            raise ValueError("raw snapshot namespace changed during durability sync")
    finally:
        os.close(raw_fd)
        os.close(root_fd)


def _verify_written_snapshot_windows(
    output_dir: str | Path,
    stored_path: str,
    expected_file_stat: os.stat_result,
) -> None:
    opened = open_managed_file_for_read(output_dir, stored_path)
    try:
        if _stable_written_identity(opened.stat_result) != _stable_written_identity(expected_file_stat):
            raise ValueError("raw snapshot changed after durable write")
    finally:
        opened.close()


def write_raw_snapshot_json(
    output_dir: str | Path,
    base_name: str,
    payload: dict,
) -> str:
    """Exclusively create one immutable JSON snapshot and return its stored path."""
    if not isinstance(payload, dict):
        raise ValueError("raw snapshot payload must be a JSON object")
    ensure_raw_snapshot_dir(output_dir)
    base = _validate_base_name(base_name)
    filename = f"{base}_{uuid4().hex}.json"
    stored_path = f"{RAW_SNAPSHOT_DIR}/{filename}"
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    opened = open_managed_file_for_create(
        output_dir,
        stored_path,
        write_through=True,
    )
    final_stat: os.stat_result | None = None
    try:
        opened.stream.write(encoded)
        opened.stream.flush()
        os.fsync(opened.stream.fileno())
        final_stat = os.fstat(opened.stream.fileno())
    finally:
        opened.close()

    if final_stat is None:
        raise ValueError("raw snapshot durability state is unavailable")
    if os.name == "nt":
        _verify_written_snapshot_windows(output_dir, stored_path, final_stat)
    else:
        _sync_raw_snapshot_parent_posix(_root(output_dir), filename, final_stat)
    return stored_path
