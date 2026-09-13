from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO


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


def _file_identity(info) -> tuple[int, int, int, int, int]:
    return (
        int(getattr(info, "st_dev", 0) or 0),
        int(getattr(info, "st_ino", 0) or 0),
        int(getattr(info, "st_size", 0) or 0),
        int(getattr(info, "st_mtime_ns", 0) or 0),
        int(getattr(info, "st_ctime_ns", 0) or 0),
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


def open_managed_file_for_read(root_value: str | Path, stored_path: str) -> BinaryIO:
    """Open a regular managed file and return the validated open handle."""
    root = _managed_root(root_value)
    relative = _validated_relative(stored_path)
    lexical = root / relative

    _assert_unlinked_components(root, relative)
    try:
        candidate = lexical.resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("managed storage path escapes root") from exc

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0) or 0)
    fd = os.open(os.fspath(lexical), flags)
    try:
        opened_info = os.fstat(fd)
        if not stat.S_ISREG(opened_info.st_mode):
            raise ValueError("managed storage path is not a regular file")

        _assert_unlinked_components(root, relative)
        try:
            current_info = lexical.lstat()
        except FileNotFoundError as exc:
            raise ValueError("managed storage file disappeared during open") from exc
        if not stat.S_ISREG(current_info.st_mode):
            raise ValueError("managed storage path is not a regular file")
        if _file_identity(opened_info) != _file_identity(current_info):
            raise ValueError("managed storage file changed during open")

        try:
            current_resolved = lexical.resolve()
            current_resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("managed storage path escapes root") from exc

        file_obj = os.fdopen(fd, "rb", closefd=True)
        fd = -1
        return file_obj
    finally:
        if fd >= 0:
            os.close(fd)


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
