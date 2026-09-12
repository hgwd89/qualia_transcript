from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import config
from models import db
from models.generated_file import GeneratedFile


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
    """Resolve one DB stored_path and reject paths outside OUTPUT_DIR."""
    normalized = str(stored_path or "").replace("\\", "/")
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("stored output path is invalid")

    root = _output_root()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("stored output path escapes OUTPUT_DIR") from exc
    return candidate


def _unique_storage_name(filename: str) -> str:
    """Return an internal storage name independent of the download filename."""
    path = Path(filename)
    suffix = path.suffix
    stem = path.name[:-len(suffix)] if suffix else path.name
    return f"{stem}_{uuid4().hex}{suffix}"


def prepare_output_target(project_id: int, filename: str) -> OutputTarget:
    """Create a safe project-scoped, collision-resistant generated-file target.

    ``filename`` remains the user-facing download name. The filesystem/DB
    ``stored_path`` uses a UUID-backed internal name so concurrent generations of
    the same report cannot overwrite one another or share rollback cleanup.
    """
    project_id = int(project_id)
    if project_id <= 0:
        raise ValueError("project_id must be positive")

    safe_name = safe_output_filename(filename)
    storage_name = _unique_storage_name(safe_name)
    stored_path = f"{project_id}/{storage_name}"
    full_path = _resolve_stored_path(stored_path)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    return OutputTarget(
        filename=safe_name,
        full_path=str(full_path),
        stored_path=stored_path,
    )


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
    target has a unique internal path, so rollback cleanup cannot delete an older
    successful generation that has the same user-facing filename.
    """
    managed_path = _resolve_stored_path(target.stored_path)
    if managed_path != Path(target.full_path).resolve():
        raise ValueError("output target path mismatch")
    if not managed_path.is_file():
        raise FileNotFoundError(f"generated output file not found: {managed_path}")

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
            managed_path.unlink(missing_ok=True)
        except OSError:
            # Preserve the original DB exception. A later integrity audit can
            # report an undeleted orphan if the filesystem itself rejected cleanup.
            pass
        raise


def get_full_path(gf: GeneratedFile) -> str:
    return str(_resolve_stored_path(gf.stored_path))


def file_exists(gf: GeneratedFile) -> bool:
    try:
        return Path(get_full_path(gf)).is_file()
    except ValueError:
        return False
