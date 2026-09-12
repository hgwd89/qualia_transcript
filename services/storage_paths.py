from __future__ import annotations

import stat
from pathlib import Path


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


def resolve_managed_path(root_value: str | Path, stored_path: str) -> Path:
    """Resolve one managed path without accepting linked/reparse components.

    The configured root itself may resolve through an operator-controlled link, but
    every path component below that resolved root must be a normal filesystem entry.
    This prevents a project/interview ID directory (or stored file) from redirecting
    reads/writes into a sibling ID directory through a symlink or Windows junction.
    """
    root = _managed_root(root_value)
    relative = _validated_relative(stored_path)
    lexical = root / relative

    current = root
    for part in relative.parts:
        current = current / part
        if is_link_or_reparse(current):
            raise ValueError("managed storage path contains a linked/reparse entry")

    try:
        candidate = lexical.resolve()
        candidate.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("managed storage path escapes root") from exc
    return candidate


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
