from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from services.local_backup import (
    DB_ARCHIVE_PATH,
    _collect_tree,
    _sha256,
    validate_backup,
)


_BACKUP_NAME_PREFIX = "qualia_backup_"
_BACKUP_TIMESTAMP_LENGTH = len("YYYYMMDDTHHMMSSZ")


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


def _entry_index(entries) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for entry in entries or []:
        path = str(entry.get("path") or "") if isinstance(entry, dict) else ""
        size = int(entry.get("size", -1)) if isinstance(entry, dict) else -1
        sha256 = str(entry.get("sha256") or "") if isinstance(entry, dict) else ""
        if not path or size < 0 or len(sha256) != 64:
            raise ValueError(f"invalid recovery manifest entry: {entry!r}")
        if path in index:
            raise ValueError(f"duplicate recovery manifest path: {path}")
        index[path] = {"path": path, "size": size, "sha256": sha256}
    return index


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
    """
    latest = _latest_backup_path(Path(backup_dir).resolve())
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
        backup_index = _entry_index(manifest.get("files") if isinstance(manifest, dict) else None)
        current_index = _entry_index(
            _current_recovery_files(connection, upload_dir, output_dir)
        )
    except Exception as exc:
        return BackupRecoverySetComparison(
            latest_backup=latest,
            validation_error=None,
            comparison_error=f"{type(exc).__name__}: {exc}",
            matches_current=None,
            backup_file_count=0,
            current_file_count=0,
            missing_from_backup=[],
            extra_in_backup=[],
            changed_files=[],
        )

    backup_paths = set(backup_index)
    current_paths = set(current_index)
    missing_from_backup = [current_index[path] for path in sorted(current_paths - backup_paths)]
    extra_in_backup = [backup_index[path] for path in sorted(backup_paths - current_paths)]
    changed_files = []
    for path in sorted(backup_paths & current_paths):
        before = backup_index[path]
        current = current_index[path]
        if before["size"] != current["size"] or before["sha256"] != current["sha256"]:
            changed_files.append({
                "path": path,
                "backup_size": before["size"],
                "current_size": current["size"],
                "backup_sha256": before["sha256"],
                "current_sha256": current["sha256"],
            })

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
