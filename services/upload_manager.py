from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from werkzeug.utils import secure_filename

import config
from models import db
from models.interview import Interview, MediaFile


@dataclass(frozen=True)
class MediaUploadTarget:
    full_path: str
    stored_path: str
    extension: str


def _upload_root() -> Path:
    return Path(config.UPLOAD_DIR).resolve()


def _resolve_stored_path(stored_path: str) -> Path:
    normalized = str(stored_path or "").replace("\\", "/")
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("stored media path is invalid")

    root = _upload_root()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("stored media path escapes UPLOAD_DIR") from exc
    return candidate


def media_extension(original_filename: str) -> str:
    safe = secure_filename(str(original_filename or ""))
    ext = Path(safe).suffix.lower()
    if not ext or ext not in config.ALLOWED_AUDIO_EXTENSIONS:
        raise ValueError("unsupported media extension")
    return ext


def prepare_media_upload_target(interview_id: int, original_filename: str) -> MediaUploadTarget:
    interview_id = int(interview_id)
    if interview_id <= 0:
        raise ValueError("interview_id must be positive")

    ext = media_extension(original_filename)
    stored_name = f"{uuid4().hex}{ext}"
    stored_path = f"{interview_id}/{stored_name}"
    full_path = _resolve_stored_path(stored_path)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    return MediaUploadTarget(
        full_path=str(full_path),
        stored_path=stored_path,
        extension=ext,
    )


def _discard_target(target: MediaUploadTarget) -> None:
    try:
        path = _resolve_stored_path(target.stored_path)
        if path == Path(target.full_path).resolve():
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def save_and_register_media(
    file_storage,
    interview: Interview,
    *,
    original_filename: str,
    mime_type: str | None = None,
) -> MediaFile:
    """Save media and atomically commit its DB metadata as far as one DB allows.

    Filesystem and database cannot share one transaction. The function therefore
    removes a partial/saved upload whenever file saving or the DB commit fails.
    The caller's pending Interview row is committed in the same DB transaction as
    the MediaFile row.
    """
    if interview.id is None:
        raise ValueError("interview must be flushed before media upload")

    target = prepare_media_upload_target(interview.id, original_filename)
    try:
        file_storage.save(target.full_path)
        full_path = Path(target.full_path)
        if not full_path.is_file():
            raise FileNotFoundError("uploaded media file was not created")

        media = MediaFile(
            interview_id=int(interview.id),
            original_filename=str(original_filename),
            stored_path=target.stored_path,
            file_type=(
                "audio"
                if target.extension in {".mp3", ".m4a", ".wav", ".ogg", ".flac"}
                else "video"
            ),
            mime_type=mime_type,
            file_size_bytes=full_path.stat().st_size,
        )
        db.session.add(media)
        db.session.commit()
        return media
    except Exception:
        db.session.rollback()
        _discard_target(target)
        raise


def get_media_full_path(media: MediaFile) -> str:
    return str(_resolve_stored_path(media.stored_path))


def media_file_exists(media: MediaFile) -> bool:
    try:
        return Path(get_media_full_path(media)).is_file()
    except ValueError:
        return False
