from __future__ import annotations

import re
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
    """Create one generated output through the ancestry-pinned write boundary.

    The callback receives an already-opened seekable binary stream. It must write
    to that stream rather than reopening ``target.full_path``. Ancestors remain
    pinned until the callback finishes and the exact created file is flushed and
    closed. A failed writer removes the partial file through the same pinned
    ancestry boundary.
    """
    opened = open_managed_file_for_write(config.OUTPUT_DIR, target.stored_path)
    try:
        writer(opened.stream)
        opened.finish()
    except Exception:
        opened.abort()
        raise


def register_generated_file(
    target: OutputTarget,
    *,
    project_id: int,
    file_type: str,
    file_format: str,
    interview_id: int | None = None,
    generation_params_json: str | None = None,
) -> GeneratedFile:
    """Register a generated file; remove only this target if DB registration fails.

    Filesystem and database commits cannot be made truly atomic. Each prepared
    target has a unique internal path. Registration reopens the file through the
    ancestry-pinned read boundary rather than trusting a check-then-reopen path.
    If the DB commit fails, cleanup uses pinned ancestry deletion rather than a
    pathname unlink that could be redirected by an ID-directory replacement.
    """
    try:
        opened = open_managed_file_for_read(config.OUTPUT_DIR, target.stored_path)
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(f"generated output file not found: {target.stored_path}") from exc
    try:
        if opened.stat_result.st_size < 0:
            raise ValueError("generated output file has invalid size")
    finally:
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
