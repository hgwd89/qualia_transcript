from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import config
from models import db
from models.interview import Interview, MediaFile
from services.managed_write_commit_guard import open_managed_write_commit_guard
from services.storage_paths import (
    ensure_managed_id_dir,
    open_managed_file_for_create,
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


def _discard_target_if_same_generation(
    target: MediaUploadTarget,
    expected: os.stat_result | None,
) -> None:
    """Best-effort cleanup through a re-pinned exact-generation guard."""
    if expected is None:
        return
    guard = None
    try:
        guard = open_managed_write_commit_guard(
            config.UPLOAD_DIR,
            target.stored_path,
            expected,
        )
        guard.discard()
    except (OSError, ValueError):
        pass
    finally:
        if guard is not None:
            guard.close()


def _compensate_committed_media(media: MediaFile) -> None:
    try:
        db.session.delete(media)
        db.session.commit()
    except Exception as cleanup_exc:
        db.session.rollback()
        raise RuntimeError(
            "uploaded media namespace changed during DB commit and compensating row deletion failed"
        ) from cleanup_exc


def save_and_register_media(
    file_storage,
    interview: Interview,
    *,
    original_filename: str,
    mime_type: str | None = None,
) -> MediaFile:
    """Save media and bind metadata to the exact bytes and generation written.

    Upload bytes are copied to the ancestry-pinned exclusive writer while SHA-256
    is computed in the same loop. After the writer closes, a commit guard re-pins
    that exact generation through the DB commit window. New rows therefore carry a
    persistent content identity in addition to filesystem-generation fencing.
    """
    if interview.id is None:
        raise ValueError("interview must be flushed before media upload")

    target = prepare_media_upload_target(interview.id, original_filename)
    written_stat = None
    content_sha256 = None
    guard = None
    media = None
    committed = False
    try:
        opened = open_managed_file_for_create(config.UPLOAD_DIR, target.stored_path)
        hasher = hashlib.sha256()
        copied = 0
        try:
            while True:
                chunk = file_storage.stream.read(1024 * 1024)
                if not chunk:
                    break
                opened.stream.write(chunk)
                hasher.update(chunk)
                copied += len(chunk)
            opened.stream.flush()
            written_stat = os.fstat(opened.stream.fileno())
            content_sha256 = hasher.hexdigest()
        finally:
            if written_stat is None and not opened.stream.closed:
                try:
                    opened.stream.flush()
                    written_stat = os.fstat(opened.stream.fileno())
                except OSError:
                    pass
            opened.close()

        if written_stat is None or content_sha256 is None:
            raise RuntimeError("uploaded media write did not complete")
        if copied != int(written_stat.st_size):
            raise ValueError("uploaded media byte count does not match written file size")

        guard = open_managed_write_commit_guard(
            config.UPLOAD_DIR,
            target.stored_path,
            written_stat,
        )
        guard.verify_namespace()

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
            file_size_bytes=int(written_stat.st_size),
            content_sha256=content_sha256,
        )
        db.session.add(media)
        db.session.commit()
        committed = True

        try:
            guard.verify_namespace()
        except Exception as namespace_exc:
            _compensate_committed_media(media)
            try:
                guard.discard()
            except (OSError, ValueError):
                pass
            raise namespace_exc
        return media
    except Exception:
        if not committed:
            db.session.rollback()
            if guard is not None:
                try:
                    guard.discard()
                except (OSError, ValueError):
                    pass
            else:
                _discard_target_if_same_generation(target, written_stat)
        raise
    finally:
        if guard is not None:
            guard.close()


def create_media_read_snapshot(media: MediaFile) -> MediaReadSnapshot:
    """Copy and verify one managed media object into a private temp pathname.

    The source is acquired through the ancestry-pinned read boundary. For new rows,
    SHA-256 and stored size are verified from that exact open handle while copying;
    a same-path replacement therefore cannot silently become transcription input.
    Legacy rows whose digest is NULL remain readable for compatibility but are not
    automatically blessed with a digest from the current pathname.
    """
    opened = open_managed_file_for_read(config.UPLOAD_DIR, media.stored_path)
    temporary_directory = None
    try:
        expected_size = media.file_size_bytes
        if expected_size is not None and int(opened.stat_result.st_size) != int(expected_size):
            raise ValueError("managed media size no longer matches registered metadata")

        temporary_directory = tempfile.TemporaryDirectory(prefix="qualia_media_read_")
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
        hasher = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb", closefd=True) as destination:
                fd = -1
                while True:
                    chunk = opened.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    destination.write(chunk)
                    hasher.update(chunk)
                    copied += len(chunk)
        finally:
            if fd >= 0:
                os.close(fd)

        after = os.fstat(opened.stream.fileno())
        if _file_generation(after) != _file_generation(opened.stat_result):
            raise ValueError("managed media changed while creating read snapshot")
        if copied != int(opened.stat_result.st_size):
            raise ValueError("managed media snapshot size mismatch")
        if expected_size is not None and copied != int(expected_size):
            raise ValueError("managed media bytes no longer match registered size")

        expected_sha256 = media.content_sha256
        if expected_sha256 is not None:
            expected_digest = str(expected_sha256).strip().lower()
            actual_digest = hasher.hexdigest()
            if len(expected_digest) != 64 or actual_digest != expected_digest:
                raise ValueError("managed media content no longer matches registered SHA-256")

        if snapshot_path.stat().st_size != copied:
            raise ValueError("managed media snapshot was not written completely")

        return MediaReadSnapshot(
            full_path=str(snapshot_path),
            _temporary_directory=temporary_directory,
        )
    except Exception:
        if temporary_directory is not None:
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
