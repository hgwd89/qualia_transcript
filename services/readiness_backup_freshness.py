from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from services.local_backup import (
    DB_ARCHIVE_PATH,
    _collect_tree,
    _safe_member_name,
    _sha256,
    validate_backup,
)


_BACKUP_NAME_PREFIX = "qualia_backup_"
_BACKUP_TIMESTAMP_LENGTH = len("YYYYMMDDTHHMMSSZ")
_DATABASE_KEY = ("database", "")


@dataclass(frozen=True)
class BackupRecoverySetComparison:
    latest_backup: Path | None
    validation_error: str | None
    comparison_error: str | None
    matches_current: bool | None
    backup_file_count: int
    current_file_count: int
    missing_from_backup: list[dict]
    extra_in_backup: list[dict]
    changed_files: list[dict]


def _backup_recency_key(path: Path) -> tuple[str, int, str]:
    """Mirror readiness backup ordering without relying on label/UUID suffixes."""
    name = path.name
    timestamp_start = len(_BACKUP_NAME_PREFIX)
    timestamp_key = name[timestamp_start:timestamp_start + _BACKUP_TIMESTAMP_LENGTH]
    try:
        mtime_ns = int(path.stat().st_mtime_ns)
    except OSError:
        mtime_ns = -1
    return timestamp_key, mtime_ns, name


def _latest_backup_path(backup_dir: Path) -> Path | None:
    archives = list(backup_dir.glob("qualia_backup_*.zip")) if backup_dir.is_dir() else []
    if not archives:
        return None
    return max(archives, key=_backup_recency_key)


def _backup_generation_token(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _backup_archive_fingerprint(path: Path) -> tuple[tuple[int, int, int, int, int, int], str]:
    """Hash one pinned archive generation and prove its pathname still names it."""
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("backup archive is not a regular file")
            opened_token = _backup_generation_token(opened)
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after_read = os.fstat(stream.fileno())
            try:
                path_after = path.stat()
            except OSError as exc:
                raise ValueError("backup archive pathname changed during fingerprint") from exc
            if (
                _backup_generation_token(after_read) != opened_token
                or _backup_generation_token(path_after) != opened_token
            ):
                raise ValueError("backup archive generation changed during fingerprint")
            return opened_token, digest.hexdigest()
    except OSError as exc:
        raise ValueError("backup archive could not be fingerprinted safely") from exc


def _snapshot_connection_database(connection: sqlite3.Connection, destination: Path) -> None:
    """Materialize the caller's already-pinned SQLite read snapshot."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(destination))
    try:
        connection.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"current database snapshot integrity_check failed: {result}")
        target.commit()
    finally:
        target.close()


def _current_recovery_files(
    connection: sqlite3.Connection,
    upload_dir: Path,
    output_dir: Path,
) -> list[dict]:
    """Snapshot the current recovery set using the same file contract as backup creation."""
    with tempfile.TemporaryDirectory(prefix="qualia_readiness_recovery_set_") as tmp:
        staging = Path(tmp).resolve()
        db_stage = staging / DB_ARCHIVE_PATH
        _snapshot_connection_database(connection, db_stage)
        files = [{
            "path": DB_ARCHIVE_PATH,
            "size": db_stage.stat().st_size,
            "sha256": _sha256(db_stage),
        }]
        files.extend(_collect_tree(Path(upload_dir).resolve(), "uploads", staging))
        files.extend(_collect_tree(Path(output_dir).resolve(), "outputs", staging))
        return files


def _entry_index(
    entries,
    *,
    database_archive_path: str | None = None,
) -> dict[tuple[str, str], dict]:
    """Index recovery members while treating the DB archive pathname as metadata.

    Restore semantics identify the database through manifest.database_archive_path;
    its archive pathname is not part of the recovered database identity. Upload and
    output member paths remain exact recovery-set identity and are compared exactly.
    """
    normalized_database_path = (
        _safe_member_name(database_archive_path)
        if database_archive_path
        else None
    )
    index: dict[tuple[str, str], dict] = {}
    normalized_paths: set[str] = set()
    for entry in entries or []:
        raw_path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
        size = int(entry.get("size", -1)) if isinstance(entry, dict) else -1
        sha256 = str(entry.get("sha256") or "") if isinstance(entry, dict) else ""
        path = _safe_member_name(raw_path) if raw_path else ""
        if not path or size < 0 or len(sha256) != 64:
            raise ValueError(f"invalid recovery manifest entry: {entry!r}")
        if path in normalized_paths:
            raise ValueError(f"duplicate recovery manifest path: {path}")
        normalized_paths.add(path)

        key = _DATABASE_KEY if path == normalized_database_path else ("file", path)
        if key in index:
            raise ValueError(f"duplicate logical recovery member: {path}")
        index[key] = {"path": path, "size": size, "sha256": sha256}
    return index


def _comparison_failure(
    latest: Path,
    error: Exception | str,
    *,
    backup_file_count: int = 0,
    current_file_count: int = 0,
) -> BackupRecoverySetComparison:
    detail = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    return BackupRecoverySetComparison(
        latest_backup=latest,
        validation_error=None,
        comparison_error=str(detail),
        matches_current=None,
        backup_file_count=backup_file_count,
        current_file_count=current_file_count,
        missing_from_backup=[],
        extra_in_backup=[],
        changed_files=[],
    )


def compare_latest_backup_to_current_recovery_set(
    connection: sqlite3.Connection,
    backup_dir: Path,
    upload_dir: Path,
    output_dir: Path,
) -> BackupRecoverySetComparison:
    """Prove whether the newest valid backup reproduces the audited recovery set.

    The database side is copied from the caller's active SQLite read transaction,
    so DB comparison is bound to the same snapshot used by readiness queries.
    Upload/output files are snapshotted with the same hardened collector used by
    backup creation. Comparison is exact on membership, byte size, and SHA-256.
    The database is compared as a logical recovery member because its ZIP member
    pathname is explicitly declared by the validated backup manifest and restore
    supports safe noncanonical database member names.

    The selected newest archive is fingerprinted before validation and again after
    recovery-set comparison. The comparison fails closed if its bytes/generation
    change or if a different archive becomes newest while readiness is running.
    """
    backup_root = Path(backup_dir).resolve()
    latest = _latest_backup_path(backup_root)
    if latest is None:
        return BackupRecoverySetComparison(
            latest_backup=None,
            validation_error=None,
            comparison_error=None,
            matches_current=None,
            backup_file_count=0,
            current_file_count=0,
            missing_from_backup=[],
            extra_in_backup=[],
            changed_files=[],
        )

    try:
        initial_generation, initial_archive_sha256 = _backup_archive_fingerprint(latest)
        manifest = validate_backup(latest)
    except Exception as exc:
        return BackupRecoverySetComparison(
            latest_backup=latest,
            validation_error=f"{type(exc).__name__}: {exc}",
            comparison_error=None,
            matches_current=None,
            backup_file_count=0,
            current_file_count=0,
            missing_from_backup=[],
            extra_in_backup=[],
            changed_files=[],
        )

    try:
        database_archive_path = _safe_member_name(
            str(manifest.get("database_archive_path") or DB_ARCHIVE_PATH)
        )
        backup_index = _entry_index(
            manifest.get("files") if isinstance(manifest, dict) else None,
            database_archive_path=database_archive_path,
        )
        current_index = _entry_index(
            _current_recovery_files(connection, upload_dir, output_dir),
            database_archive_path=DB_ARCHIVE_PATH,
        )
        if _DATABASE_KEY not in backup_index or _DATABASE_KEY not in current_index:
            raise ValueError("recovery set has no logical database member")
    except Exception as exc:
        return _comparison_failure(latest, exc)

    backup_keys = set(backup_index)
    current_keys = set(current_index)
    missing_from_backup = [current_index[key] for key in sorted(current_keys - backup_keys)]
    extra_in_backup = [backup_index[key] for key in sorted(backup_keys - current_keys)]
    changed_files = []
    for key in sorted(backup_keys & current_keys):
        before = backup_index[key]
        current = current_index[key]
        if before["size"] != current["size"] or before["sha256"] != current["sha256"]:
            changed_files.append({
                "path": before["path"] if key == _DATABASE_KEY else current["path"],
                "logical_member": "database" if key == _DATABASE_KEY else "file",
                "backup_size": before["size"],
                "current_size": current["size"],
                "backup_sha256": before["sha256"],
                "current_sha256": current["sha256"],
            })

    try:
        latest_after = _latest_backup_path(backup_root)
        if latest_after is None or latest_after.resolve() != latest.resolve():
            raise ValueError("newest backup selection changed during readiness comparison")
        final_generation, final_archive_sha256 = _backup_archive_fingerprint(latest_after)
        if (
            final_generation != initial_generation
            or final_archive_sha256 != initial_archive_sha256
        ):
            raise ValueError("newest backup archive changed during readiness comparison")
    except Exception as exc:
        return _comparison_failure(
            latest,
            exc,
            backup_file_count=len(backup_index),
            current_file_count=len(current_index),
        )

    matches = not missing_from_backup and not extra_in_backup and not changed_files
    return BackupRecoverySetComparison(
        latest_backup=latest,
        validation_error=None,
        comparison_error=None,
        matches_current=matches,
        backup_file_count=len(backup_index),
        current_file_count=len(current_index),
        missing_from_backup=missing_from_backup,
        extra_in_backup=extra_in_backup,
        changed_files=changed_files,
    )