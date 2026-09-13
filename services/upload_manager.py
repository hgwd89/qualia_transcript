from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import config
from models import db
from models.interview import Interview, MediaFile
from services.storage_paths import (
    ensure_managed_id_dir,
    open_managed_file_for_read,
    resolve_managed_path,
)


@dataclass(frozen=True)
class MediaUploadTarget:
    full_path: str
    stored_path: str
    extension: str


@dataclass
class MediaReadSnapshot:
    """Private pathname snapshot copied from one ancestry-pinned managed read."""

    full_path: str
    _temporary_directory: tempfile.TemporaryDirectory
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._temporary_directory.cleanup()
        self._closed = True

    def __enter__(self) -> "MediaReadSnapshot":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _upload_root() -> Path:
    return Path(config.UPLOAD_DIR).resolve()


def _resolve_stored_path(stored_path: str) -> Path:
    return resolve_managed_path(config.UPLOAD_DIR, stored_path)


def _file_generation(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def media_extension(original_filename: str) -> str:
    # The original name is metadata only; the stored filename is always a UUID.
    # Inspect the original Unicode extension directly so names such as
    # "インタビュー音声.mp3" do not lose their suffix through ASCII sanitization.
    normalized = str(original_filename or "").replace("\\", "/")
    basename = normalized.rsplit("/", 1)[-1]
    ext = Path(basename).suffix.lower()
    if not ext or ext not in config.ALLOWED_AUDIO_EXTENSIONS:
        raise ValueError("unsupported media extension")
    return ext


def prepare_media_upload_target(interview_id: int, original_filename: str) -> MediaUploadTarget:
    interview_id = int(interview_id)
    if interview_id <= 0:
        raise ValueError("interview_id must be positive")

    ext = media_extension(original_filename)
    stored_name = f"{uuid4().hex}{ext}"
    ensure_managed_id_dir(config.UPLOAD_DIR, interview_id)
    stored_path = f"{interview_id}/{stored_name}"
    full_path = _resolve_stored_path(stored_path)
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
    the MediaFile row. Interview ID directories must be normal directories rather
    than symlinks or Windows junction/reparse points.
    """
    if interview.id is None:
        raise ValueError("interview must be flushed before media upload")

    target = prepare_media_upload_target(interview.id, original_filename)
    try:
        file_storage.save(target.full_path)
        full_path = _resolve_stored_path(target.stored_path)
        if full_path != Path(target.full_path).resolve():
            raise ValueError("uploaded media target path changed")
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


def create_media_read_snapshot(media: MediaFile) -> MediaReadSnapshot:
    """Copy managed media once from a pinned handle into a private temp pathname.

    PyAV, OpenAI's upload client, and faster-whisper all accept pathnames and may
    reopen them internally. The managed upload pathname is therefore resolved and
    opened exactly once through the ancestry-pinned storage boundary. Downstream
    consumers receive only the private snapshot path, so later replacement of the
    managed pathname cannot redirect an in-flight transcription.
    """
    opened = open_managed_file_for_read(config.UPLOAD_DIR, media.stored_path)
    temporary_directory = tempfile.TemporaryDirectory(prefix="qualia_media_read_")
    try:
        temp_root = Path(temporary_directory.name)
        try:
            os.chmod(temp_root, 0o700)
        except OSError:
            pass

        suffix = Path(media.stored_path).suffix.lower() or ".media"
        snapshot_path = temp_root / f"source{suffix}"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | int(getattr(os, "O_BINARY", 0) or 0)
        fd = os.open(snapshot_path, flags, 0o600)
        copied = 0
        try:
            with os.fdopen(fd, "wb", closefd=True) as destination:
                fd = -1
                while True:
                    chunk = opened.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    copied += len(chunk)
        finally:
            if fd >= 0:
                os.close(fd)

        after = os.fstat(opened.stream.fileno())
        if _file_generation(after) != _file_generation(opened.stat_result):
            raise ValueError("managed media changed while creating read snapshot")
        if copied != int(opened.stat_result.st_size):
            raise ValueError("managed media snapshot size mismatch")
        if snapshot_path.stat().st_size != copied:
            raise ValueError("managed media snapshot was not written completely")

        return MediaReadSnapshot(
            full_path=str(snapshot_path),
            _temporary_directory=temporary_directory,
        )
    except Exception:
        temporary_directory.cleanup()
        raise
    finally:
        opened.close()


def get_media_full_path(media: MediaFile) -> str:
    return str(_resolve_stored_path(media.stored_path))


def media_file_exists(media: MediaFile) -> bool:
    try:
        return Path(get_media_full_path(media)).is_file()
    except ValueError:
        return False
