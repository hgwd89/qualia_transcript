"""Verified local backup/restore for Qualia Transcript.

Backups contain a consistent SQLite snapshot plus uploads/ and outputs/, with a
SHA-256 manifest. Restore validates the entire archive before touching targets.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import config


BACKUP_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DB_ARCHIVE_PATH = "database/qualia_transcript.db"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_path(database_uri: str | None = None) -> Path:
    uri = database_uri or config.DATABASE_URI
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise ValueError("local backup currently supports sqlite:/// databases only")
    raw = uri[len(prefix):]
    if raw in {"", ":memory:"}:
        raise ValueError("file-backed SQLite database is required")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(config.BASE_DIR) / path
    return path.resolve()


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    p = PurePosixPath(normalized)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe archive path: {name}")
    if not p.parts:
        raise ValueError("empty archive path")
    return p.as_posix()


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database not found: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_uri = source.as_uri() + "?mode=ro"
    src = sqlite3.connect(source_uri, uri=True, timeout=10)
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"backup database integrity_check failed: {result}")
        dst.commit()
    finally:
        dst.close()
        src.close()


def _collect_tree(source_dir: Path, archive_prefix: str, staging_root: Path) -> list[dict]:
    entries: list[dict] = []
    if not source_dir.exists():
        return entries
    if not source_dir.is_dir():
        raise ValueError(f"expected directory: {source_dir}")

    for source in sorted(source_dir.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_dir).as_posix()
        archive_path = _safe_member_name(f"{archive_prefix}/{relative}")
        staged = staging_root / Path(archive_path)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, staged)
        entries.append({
            "path": archive_path,
            "size": staged.stat().st_size,
            "sha256": _sha256(staged),
        })
    return entries


def create_backup(
    destination_dir: str | os.PathLike | None = None,
    *,
    database_uri: str | None = None,
    upload_dir: str | os.PathLike | None = None,
    output_dir: str | os.PathLike | None = None,
    label: str = "manual",
) -> Path:
    """Create and verify a single ZIP backup archive."""
    database_path = _sqlite_path(database_uri)
    uploads = Path(upload_dir or config.UPLOAD_DIR).resolve()
    outputs = Path(output_dir or config.OUTPUT_DIR).resolve()
    destination = Path(destination_dir or config.BACKUP_DIR).resolve()
    destination.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in label)[:40] or "backup"
    archive = destination / f"qualia_backup_{ts}_{safe_label}_{uuid.uuid4().hex[:8]}.zip"

    with tempfile.TemporaryDirectory(prefix="qualia_backup_stage_") as tmp:
        staging = Path(tmp)
        db_stage = staging / DB_ARCHIVE_PATH
        _sqlite_snapshot(database_path, db_stage)

        files = [{
            "path": DB_ARCHIVE_PATH,
            "size": db_stage.stat().st_size,
            "sha256": _sha256(db_stage),
        }]
        files.extend(_collect_tree(uploads, "uploads", staging))
        files.extend(_collect_tree(outputs, "outputs", staging))

        manifest = {
            "format": "qualia-transcript-backup",
            "format_version": BACKUP_FORMAT_VERSION,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "label": label,
            "service_slug": config.SERVICE_SLUG,
            "database_archive_path": DB_ARCHIVE_PATH,
            "files": files,
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for source in sorted(staging.rglob("*")):
                if source.is_file():
                    zf.write(source, source.relative_to(staging).as_posix())

    validate_backup(archive)
    return archive


def validate_backup(archive_path: str | os.PathLike) -> dict:
    """Validate paths, exact manifest membership, hashes and SQLite integrity."""
    archive = Path(archive_path).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"backup archive not found: {archive}")

    with tempfile.TemporaryDirectory(prefix="qualia_backup_validate_") as tmp:
        root = Path(tmp)
        with zipfile.ZipFile(archive, "r") as zf:
            file_infos = [info for info in zf.infolist() if not info.is_dir()]
            names = [_safe_member_name(info.filename) for info in file_infos]
            if len(names) != len(set(names)):
                raise ValueError("backup contains duplicate file paths")
            if MANIFEST_NAME not in names:
                raise ValueError("backup manifest is missing")

            try:
                manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, KeyError) as exc:
                raise ValueError("backup manifest is invalid") from exc

            if manifest.get("format") != "qualia-transcript-backup":
                raise ValueError("unsupported backup format")
            if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
                raise ValueError("unsupported backup format version")

            entries = manifest.get("files")
            if not isinstance(entries, list) or not entries:
                raise ValueError("backup manifest contains no files")

            declared_paths = []
            for entry in entries:
                declared_paths.append(_safe_member_name(str(entry.get("path") or "")))
            if len(declared_paths) != len(set(declared_paths)):
                raise ValueError("backup manifest contains duplicate file paths")

            expected_archive_paths = set(declared_paths) | {MANIFEST_NAME}
            if set(names) != expected_archive_paths:
                extra = sorted(set(names) - expected_archive_paths)
                missing = sorted(expected_archive_paths - set(names))
                raise ValueError(f"archive/manifest membership mismatch: extra={extra}, missing={missing}")

            for info in file_infos:
                safe_name = _safe_member_name(info.filename)
                target = (root / Path(safe_name)).resolve()
                if root not in target.parents and target != root:
                    raise ValueError(f"unsafe archive member: {info.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

        for entry in entries:
            archive_name = _safe_member_name(str(entry.get("path") or ""))
            path = root / archive_name
            if not path.is_file():
                raise ValueError(f"manifest file missing from archive: {archive_name}")
            if path.stat().st_size != int(entry.get("size", -1)):
                raise ValueError(f"size mismatch: {archive_name}")
            if _sha256(path) != entry.get("sha256"):
                raise ValueError(f"sha256 mismatch: {archive_name}")

        db_name = _safe_member_name(str(manifest.get("database_archive_path") or DB_ARCHIVE_PATH))
        db_path = root / db_name
        if not db_path.is_file():
            raise ValueError("backup database is missing")
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        try:
            result = con.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise ValueError(f"backup database integrity_check failed: {result}")
        finally:
            con.close()

        return manifest


def _copy_tree_from_stage(stage: Path, prefix: str, destination: Path) -> None:
    source = stage / prefix
    destination.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for item in source.rglob("*"):
        rel = item.relative_to(source)
        target = destination / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def restore_backup(
    archive_path: str | os.PathLike,
    *,
    apply: bool = False,
    database_uri: str | None = None,
    upload_dir: str | os.PathLike | None = None,
    output_dir: str | os.PathLike | None = None,
    backup_dir: str | os.PathLike | None = None,
    create_pre_restore_backup: bool = True,
) -> dict:
    """Validate a backup and optionally restore it.

    `apply=False` is deliberately the default. Callers must explicitly request
    destructive restore. When applying, a pre-restore backup is created first.
    The Qualia app must be stopped before a restore is applied.
    """
    manifest = validate_backup(archive_path)
    if not apply:
        return {"validated": True, "restored": False, "manifest": manifest}

    database_path = _sqlite_path(database_uri)
    uploads = Path(upload_dir or config.UPLOAD_DIR).resolve()
    outputs = Path(output_dir or config.OUTPUT_DIR).resolve()
    pre_restore_archive = None

    if create_pre_restore_backup and database_path.exists():
        pre_restore_archive = create_backup(
            backup_dir or config.BACKUP_DIR,
            database_uri=database_uri,
            upload_dir=uploads,
            output_dir=outputs,
            label="pre_restore",
        )

    with tempfile.TemporaryDirectory(prefix="qualia_restore_stage_") as tmp:
        stage = Path(tmp)
        with zipfile.ZipFile(Path(archive_path).resolve(), "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                safe_name = _safe_member_name(info.filename)
                target = stage / safe_name
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

        staged_db = stage / DB_ARCHIVE_PATH
        if not staged_db.is_file():
            raise ValueError("staged backup database missing")

        if database_path.exists():
            current = sqlite3.connect(str(database_path), timeout=1)
            try:
                current.execute("BEGIN EXCLUSIVE")
                current.rollback()
            except sqlite3.OperationalError as exc:
                raise RuntimeError("database is busy; stop Qualia Transcript before restore") from exc
            finally:
                current.close()

        rollback_root = stage / "rollback"
        rollback_root.mkdir(parents=True, exist_ok=True)
        rollback_db = rollback_root / "database.db"
        rollback_uploads = rollback_root / "uploads"
        rollback_outputs = rollback_root / "outputs"

        if database_path.exists():
            shutil.copy2(database_path, rollback_db)
        if uploads.exists():
            shutil.copytree(uploads, rollback_uploads)
        if outputs.exists():
            shutil.copytree(outputs, rollback_outputs)

        try:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_db, database_path)

            if uploads.exists():
                shutil.rmtree(uploads)
            if outputs.exists():
                shutil.rmtree(outputs)
            _copy_tree_from_stage(stage, "uploads", uploads)
            _copy_tree_from_stage(stage, "outputs", outputs)

            restored = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
            try:
                result = restored.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"restored database integrity_check failed: {result}")
            finally:
                restored.close()
        except Exception:
            if rollback_db.exists():
                shutil.copy2(rollback_db, database_path)
            if uploads.exists():
                shutil.rmtree(uploads)
            if outputs.exists():
                shutil.rmtree(outputs)
            if rollback_uploads.exists():
                shutil.copytree(rollback_uploads, uploads)
            else:
                uploads.mkdir(parents=True, exist_ok=True)
            if rollback_outputs.exists():
                shutil.copytree(rollback_outputs, outputs)
            else:
                outputs.mkdir(parents=True, exist_ok=True)
            raise

    return {
        "validated": True,
        "restored": True,
        "manifest": manifest,
        "pre_restore_backup": str(pre_restore_archive) if pre_restore_archive else None,
    }
