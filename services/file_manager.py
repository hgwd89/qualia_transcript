from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import config
from models import db
from models.generated_file import GeneratedFile
from services.storage_paths import (
    ManagedWriteFile,
    ensure_managed_id_dir,
    open_managed_file_for_create,
    resolve_managed_path,
)


_INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_STEMS = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass
class OutputTarget:
    filename: str
    full_path: str
    stored_path: str
    written_stat: os.stat_result | None = None


@dataclass
class OutputWriteFile:
    """Managed writer that records the exact final file generation on close."""

    managed: ManagedWriteFile
    target: OutputTarget
    _closed: bool = False

    @property
    def stream(self):
        return self.managed.stream

    @property
    def stat_result(self):
        return self.managed.stat_result

    def close(self) -> None:
        if self._closed:
            return
        try:
            if not self.managed.stream.closed:
                self.managed.stream.flush()
                self.target.written_stat = os.fstat(self.managed.stream.fileno())
        finally:
            self.managed.close()
            self._closed = True

    def __enter__(self) -> "OutputWriteFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


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


def _discard_output_target_if_same_generation(target: OutputTarget) -> None:
    """Best-effort cleanup without deleting a pathname-replacement successor."""
    expected = target.written_stat
    if expected is None:
        return
    try:
        managed_path = _resolve_stored_path(target.stored_path)
        if managed_path != Path(target.full_path).resolve() or not managed_path.is_file():
            return
        if _file_generation(managed_path.stat()) != _file_generation(expected):
            return
        managed_path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


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


def open_output_target_for_write(target: OutputTarget) -> OutputWriteFile:
    """Exclusively create one prepared output through the pinned write boundary."""
    managed_path = _resolve_stored_path(target.stored_path)
    if managed_path != Path(target.full_path).resolve():
        raise ValueError("output target path mismatch")
    managed = open_managed_file_for_create(config.OUTPUT_DIR, target.stored_path)
    return OutputWriteFile(managed=managed, target=target)


def register_generated_file(
    target: OutputTarget,
    *,
    project_id: int,
    file_type: str,
    file_format: str,
    interview_id: int | None = None,
    generation_params_json: str | None = None,
) -> GeneratedFile:
    """Register only the exact file generation produced by the managed writer.

    Filesystem and database commits cannot be truly atomic. The final writer
    ``fstat`` is therefore carried into this boundary so a pathname replacement
    after close cannot cause a different file to be registered or deleted during
    rollback cleanup.
    """
    expected = target.written_stat
    if expected is None:
        raise ValueError("generated output writer did not record final file identity")

    try:
        managed_path = _resolve_stored_path(target.stored_path)
        if managed_path != Path(target.full_path).resolve():
            raise ValueError("output target path mismatch")
        if not managed_path.is_file():
            raise FileNotFoundError(f"generated output file not found: {managed_path}")
        if _file_generation(managed_path.stat()) != _file_generation(expected):
            raise ValueError("generated output target changed after write")

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
        return gf
    except Exception:
        db.session.rollback()
        _discard_output_target_if_same_generation(target)
        raise


def get_full_path(gf: GeneratedFile) -> str:
    return str(_resolve_stored_path(gf.stored_path))


def file_exists(gf: GeneratedFile) -> bool:
    try:
        return Path(get_full_path(gf)).is_file()
    except ValueError:
        return False
