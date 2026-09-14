"""Read-only source-media integrity checks for professional readiness."""
from __future__ import annotations

from pathlib import Path

from services.formal_artifact_integrity import (
    FormalArtifactIntegrityError,
    verified_artifact_snapshot,
)
from services.storage_paths import open_managed_file_for_read


def _field(row, name: str):
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return None


def media_file_integrity_status(row, upload_dir: Path) -> tuple[str, str | None]:
    """Return ``verified``, ``unproven`` or ``invalid`` for one MediaFile row.

    The managed path is opened through the ancestry-pinned storage boundary before
    compatibility decisions are made. Missing/unsafe files therefore fail closed
    even for legacy rows without a digest. New rows are bound to the SHA-256 saved
    at upload time; legacy rows whose digest is NULL remain readable but cannot be
    certified as the original uploaded bytes.
    """
    stored_path = str(_field(row, "stored_path") or "")
    opened = None
    verified = None
    try:
        opened = open_managed_file_for_read(upload_dir, stored_path)

        raw_size = _field(row, "file_size_bytes")
        if raw_size is not None:
            try:
                expected_size = int(raw_size)
            except (TypeError, ValueError):
                return "invalid", "registered media size metadata is invalid"
            if expected_size < 0 or int(opened.stat_result.st_size) != expected_size:
                return "invalid", "managed media size no longer matches registered metadata"

        raw_sha256 = _field(row, "content_sha256")
        if raw_sha256 in {None, ""}:
            return "unproven", "media SHA-256 metadata is absent on this legacy row"

        expected_sha256 = str(raw_sha256).strip().lower()
        if len(expected_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha256):
            return "invalid", "registered media SHA-256 metadata is invalid"

        try:
            verified = verified_artifact_snapshot(opened, expected_sha256)
            opened = None
        except Exception:
            # verified_artifact_snapshot owns and closes the managed handle even
            # when verification raises.
            opened = None
            raise
        return "verified", None
    except (FormalArtifactIntegrityError, OSError, ValueError) as exc:
        reason = str(exc).replace("formal artifact", "managed media")
        return "invalid", f"{type(exc).__name__}: {reason}"
    finally:
        if opened is not None:
            opened.close()
        if verified is not None:
            verified.close()
