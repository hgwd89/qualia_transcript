from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable
from uuid import uuid4

import config
from models import db
from models.generated_file import GeneratedFile
from services.managed_storage_write import (
    open_managed_file_for_write,
    unlink_managed_file,
)
from services.storage_paths import (
    ensure_managed_id_dir,
    open_managed_file_for_read,
    resolve_managed_path,
)


_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_STEMS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True)
class OutputTarget:
    filename: str
    full_path: str
    stored_path: str


def _output_root() -> Path:
    return Path(config.OUTPUT_DIR).resolve()


def safe_output_filename(filename: str) -> str:
    """Return a single cross-platform filename while preserving Japanese text."""
    value = str(filename or "").strip()
    value = _INVALID_FILENAME_RE.sub("_", value)
    value = value.strip(" .")
    if not value or value in {".", ".."}:
        raise ValueError("output filename is empty or invalid")

    stem = value.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED_STEMS:
        value = f"_{value}"
    return value


def _resolve_stored_path(stored_path: str) -> Path:
    """Resolve one DB stored_path and reject linked paths/out-of-root targets."""
    return resolve_managed_path(config.OUTPUT_DIR, stored_path)


def _unique_storage_name(filename: str) -> str:
    """Return a short internal storage name independent of the download filename."""
    suffix = Path(filename).suffix
    return f"{uuid4().hex}{suffix}"


def _file_generation(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def prepare_output_target(project_id: int, filename: str) -> OutputTarget:
    """Create a safe project-scoped, collision-resistant generated-file target.

    ``filename`` remains the user-facing download name. The filesystem/DB
    ``stored_path`` uses a UUID-only internal basename plus the original extension,
    so concurrent generations cannot collide and long display names cannot push the
    filesystem component beyond common 255-byte limits. The project ID directory
    must be a normal directory, never a symlink or Windows junction/reparse point.
    """
    project_id = int(project_id)
    if project_id <= 0:
        raise ValueError("project_id must be positive")

    safe_name = safe_output_filename(filename)
    storage_name = _unique_storage_name(safe_name)
    ensure_managed_id_dir(config.OUTPUT_DIR, project_id)
    stored_path = f"{project_id}/{storage_name}"
    full_path = _resolve_stored_path(stored_path)
    return OutputTarget(
        filename=safe_name,
        full_path=str(full_path),
        stored_path=stored_path,
    )


def write_output_target(
    target: OutputTarget,
    writer: Callable[[BinaryIO], None],
) -> None:
    """Low-level managed write helper for temporary/self-contained callers.

    Formal generated outputs should use ``write_and_register_generated_file`` so
    the same opened file and pinned ancestor chain remain live through DB commit.
    """
    opened = open_managed_file_for_write(config.OUTPUT_DIR, target.stored_path)
    try:
        writer(opened.stream)
        opened.finish()
    except Exception:
        opened.abort()
        raise


def _verify_current_target(target: OutputTarget, expected: os.stat_result) -> None:
    """Fail if the logical managed pathname no longer names the created object."""
    try:
        current = open_managed_file_for_read(config.OUTPUT_DIR, target.stored_path)
    except (OSError, ValueError) as exc:
        raise ValueError("generated output pathname changed during generation") from exc
    try:
        if _file_generation(current.stat_result) != _file_generation(expected):
            raise ValueError("generated output pathname changed during generation")
    finally:
        current.close()


def write_and_register_generated_file(
    target: OutputTarget,
    writer: Callable[[BinaryIO], None],
    *,
    project_id: int,
    file_type: str,
    file_format: str,
    interview_id: int | None = None,
    generation_params_json: str | None = None,
) -> GeneratedFile:
    """Write and register one output without reopening an unpinned pathname.

    The writer receives the exact newly-created regular file while every managed
    ancestor is pinned. The file is flushed and fsynced, the current logical
    pathname must still resolve to that same file generation, and the DB row is
    committed before the write handle/ancestor pins are released. If writing,
    verification, or DB commit fails, the partial file is removed through the same
    pinned parent rather than through a later pathname lookup.
    """
    opened = open_managed_file_for_write(config.OUTPUT_DIR, target.stored_path)
    committed = False
    try:
        writer(opened.stream)
        opened.stream.flush()
        os.fsync(opened.stream.fileno())
        written = os.fstat(opened.stream.fileno())
        if not stat.S_ISREG(written.st_mode):
            raise ValueError("generated output target is not a regular file")

        _verify_current_target(target, written)

        gf = GeneratedFile(
            project_id=int(project_id),
            interview_id=interview_id,
            file_type=file_type,
            file_format=file_format,
            original_filename=target.filename,
            stored_path=target.stored_path,
            generation_params_json=generation_params_json,
        )
        db.session.add(gf)
        db.session.commit()
        committed = True
    except Exception:
        db.session.rollback()
        opened.abort()
        raise

    # The bytes were already flushed/fsynced before the DB commit. Once the row is
    # committed, never delete the file merely because final handle close reports an
    # error; that would turn a close anomaly into a committed dangling DB row.
    try:
        opened.finish()
    except Exception:
        if not committed:
            raise
    return gf


def register_generated_file(
    target: OutputTarget,
    *,
    project_id: int,
    file_type: str,
    file_format: str,
    interview_id: int | None = None,
    generation_params_json: str | None = None,
) -> GeneratedFile:
    """Compatibility helper for already-written temporary/self-contained callers.

    Production report generators must not split writing from registration; they use
    ``write_and_register_generated_file``. This helper remains only for existing
    non-racy smoke fixtures and legacy internal callers while migration completes.
    """
    try:
        opened = open_managed_file_for_read(config.OUTPUT_DIR, target.stored_path)
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(f"generated output file not found: {target.stored_path}") from exc
    opened.close()

    gf = GeneratedFile(
        project_id=int(project_id),
        interview_id=interview_id,
        file_type=file_type,
        file_format=file_format,
        original_filename=target.filename,
        stored_path=target.stored_path,
        generation_params_json=generation_params_json,
    )
    try:
        db.session.add(gf)
        db.session.commit()
        return gf
    except Exception:
        db.session.rollback()
        try:
            unlink_managed_file(config.OUTPUT_DIR, target.stored_path)
        except (OSError, ValueError):
            pass
        raise


def get_full_path(gf: GeneratedFile) -> str:
    return str(_resolve_stored_path(gf.stored_path))


def file_exists(gf: GeneratedFile) -> bool:
    try:
        return Path(get_full_path(gf)).is_file()
    except ValueError:
        return False
