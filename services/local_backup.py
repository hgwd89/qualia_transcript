"""Verified local backup/restore for Qualia Transcript.

Backups contain a consistent SQLite snapshot plus uploads/ and outputs/, with a
SHA-256 manifest. Restore stages one private archive copy, validates that copy,
and restores from those same staged bytes before touching targets.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import config
from services.storage_paths import is_link_or_reparse


BACKUP_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DB_ARCHIVE_PATH = "database/qualia_transcript.db"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _restrict_permissions(path: Path, mode: int) -> None:
    """Enforce owner-only backup permissions where POSIX mode bits are meaningful."""
    if os.name == "nt":
        return
    os.chmod(path, mode)


def _sqlite_path(database_uri: str | None = None) -> Path:
    uri = database_uri or config.DATABASE_URI
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise ValueError("local backup currently supports sqlite:/// databases only")
    raw = unquote(uri[len(prefix):])
    if raw in {"", ":memory:"}:
        raise ValueError("file-backed SQLite database is required")
    path = Path(raw)
    if not path.is_absolute():
        # Flask-SQLAlchemy resolves relative sqlite:/// paths beneath Flask's
        # instance directory. Mirror that rule for explicit legacy relative URIs.
        path = Path(getattr(config, "INSTANCE_DIR", Path(config.BASE_DIR) / "instance")) / path
    return path.resolve()


def _touches_live_recovery_set(
    database_uri: str | None,
    upload_dir: str | os.PathLike | None,
    output_dir: str | os.PathLike | None,
) -> bool:
    """Return True when any requested target is part of the configured live set."""
    database_path = _sqlite_path(database_uri)
    uploads = Path(upload_dir or config.UPLOAD_DIR).resolve()
    outputs = Path(output_dir or config.OUTPUT_DIR).resolve()
    return (
        database_path == Path(config.DATABASE_PATH).resolve()
        or uploads == Path(config.UPLOAD_DIR).resolve()
        or outputs == Path(config.OUTPUT_DIR).resolve()
    )


def _safe_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    p = PurePosixPath(normalized)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe archive path: {name}")
    if not p.parts:
        raise ValueError("empty archive path")
    return p.as_posix()


def _is_within(root: Path, target: Path) -> bool:
    """Return True when target resolves to root or a descendant of root."""
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    return resolved_target == resolved_root or resolved_root in resolved_target.parents


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

    root = source_dir.resolve()
    pending = [root]
    while pending:
        current = pending.pop()
        if current != root:
            try:
                current_info = current.lstat()
                if is_link_or_reparse(current) or not stat.S_ISDIR(current_info.st_mode):
                    raise ValueError(
                        f"managed backup directory changed during collection: {current}"
                    )
                current.resolve().relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                if isinstance(exc, ValueError) and "managed backup directory changed" in str(exc):
                    raise
                raise ValueError(f"managed backup directory is unsafe: {current}") from exc

        for source in sorted(current.iterdir(), key=lambda path: path.name):
            if is_link_or_reparse(source):
                raise ValueError(
                    f"managed backup tree contains linked/reparse entry: {source}"
                )
            try:
                info = source.lstat()
                resolved = source.resolve()
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(
                    f"managed backup tree escapes configured root: {source}"
                ) from exc

            if stat.S_ISDIR(info.st_mode):
                pending.append(source)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"managed backup tree contains unsupported entry type: {source}"
                )

            relative = source.relative_to(root).as_posix()
            archive_path = _safe_member_name(f"{archive_prefix}/{relative}")
            staged = staging_root / Path(archive_path)
            _copy_regular_snapshot_file(source, staged)
            entries.append({
                "path": archive_path,
                "size": staged.stat().st_size,
                "sha256": _sha256(staged),
            })
    return entries


def _stat_identity(info: os.stat_result) -> tuple[int, int]:
    return int(info.st_dev), int(info.st_ino)


def _apply_snapshot_metadata(target: Path, info: os.stat_result) -> None:
    if os.name != "nt":
        os.chmod(target, stat.S_IMODE(info.st_mode))
    os.utime(target, ns=(int(info.st_atime_ns), int(info.st_mtime_ns)))


def _copy_regular_snapshot_file(source: Path, target: Path) -> None:
    """Copy one regular file through a descriptor fenced to the checked entry."""
    try:
        before = source.lstat()
    except OSError as exc:
        raise ValueError(f"managed restore rollback file is unreadable: {source}") from exc
    if is_link_or_reparse(source) or not stat.S_ISREG(before.st_mode):
        raise ValueError(f"managed restore rollback file is not a regular file: {source}")

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise ValueError(f"managed restore rollback file could not be opened safely: {source}") from exc

    try:
        opened = os.fstat(fd)
        try:
            after = source.lstat()
        except OSError as exc:
            raise ValueError(f"managed restore rollback file changed during snapshot: {source}") from exc
        if (
            not stat.S_ISREG(opened.st_mode)
            or is_link_or_reparse(source)
            or _stat_identity(before) != _stat_identity(opened)
            or _stat_identity(after) != _stat_identity(opened)
        ):
            raise ValueError(f"managed restore rollback file changed during snapshot: {source}")

        target.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.dup(fd), "rb") as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        _apply_snapshot_metadata(target, opened)
    finally:
        os.close(fd)


def _snapshot_tree_no_links(source_dir: Path, destination: Path) -> None:
    """Copy a managed tree for rollback without following linked/reparse entries."""
    if not source_dir.exists():
        return
    if not source_dir.is_dir():
        raise ValueError(f"expected directory: {source_dir}")

    root = source_dir.resolve()
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise ValueError(f"managed restore rollback root is unreadable: {source_dir}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise ValueError(f"managed restore rollback root is not a directory: {source_dir}")

    destination.mkdir(parents=True, exist_ok=True)
    directory_metadata: list[tuple[Path, os.stat_result]] = [(destination, root_info)]
    pending = [root]
    while pending:
        current = pending.pop()
        if current != root:
            try:
                current_info = current.lstat()
                if is_link_or_reparse(current) or not stat.S_ISDIR(current_info.st_mode):
                    raise ValueError(
                        f"managed restore rollback directory changed during snapshot: {current}"
                    )
                current.resolve().relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                if isinstance(exc, ValueError) and "managed restore rollback directory" in str(exc):
                    raise
                raise ValueError(
                    f"managed restore rollback directory is unsafe: {current}"
                ) from exc

        for source in sorted(current.iterdir(), key=lambda path: path.name):
            if is_link_or_reparse(source):
                raise ValueError(
                    f"managed restore rollback tree contains linked/reparse entry: {source}"
                )
            try:
                info = source.lstat()
                resolved = source.resolve()
                resolved.relative_to(root)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(
                    f"managed restore rollback tree escapes configured root: {source}"
                ) from exc

            relative = source.relative_to(root)
            target = destination / relative
            if stat.S_ISDIR(info.st_mode):
                target.mkdir(parents=True, exist_ok=True)
                directory_metadata.append((target, info))
                pending.append(source)
            elif stat.S_ISREG(info.st_mode):
                _copy_regular_snapshot_file(source, target)
            else:
                raise ValueError(
                    f"managed restore rollback tree contains unsupported entry type: {source}"
                )

    for target, info in sorted(
        directory_metadata,
        key=lambda pair: len(pair[0].parts),
        reverse=True,
    ):
        _apply_snapshot_metadata(target, info)


def _create_private_partial_archive(destination: Path, archive: Path) -> Path:
    """Reserve an owner-private unpublished archive path on the destination filesystem."""
    partial = destination / f".{archive.name}.{uuid.uuid4().hex}.partial"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | int(getattr(os, "O_BINARY", 0))
    )
    fd = os.open(partial, flags, 0o600)
    os.close(fd)
    _restrict_permissions(partial, 0o600)
    return partial


def _create_backup_unlocked(
    destination_dir: str | os.PathLike | None = None,
    *,
    database_uri: str | None = None,
    upload_dir: str | os.PathLike | None = None,
    output_dir: str | os.PathLike | None = None,
    label: str = "manual",
) -> Path:
    database_path = _sqlite_path(database_uri)
    uploads = Path(upload_dir or config.UPLOAD_DIR).resolve()
    outputs = Path(output_dir or config.OUTPUT_DIR).resolve()
    destination = Path(destination_dir or config.BACKUP_DIR).resolve()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_permissions(destination, 0o700)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_label = "".join(c if c.isalnum() or c in {"-", "_"} else "_" for c in label)[:40] or "backup"
    archive = destination / f"qualia_backup_{ts}_{safe_label}_{uuid.uuid4().hex[:8]}.zip"
    partial_archive = _create_private_partial_archive(destination, archive)
    published = False

    try:
        with tempfile.TemporaryDirectory(prefix="qualia_backup_stage_") as tmp:
            staging = Path(tmp).resolve()
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

            with zipfile.ZipFile(partial_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for source in sorted(staging.rglob("*")):
                    if source.is_file():
                        zf.write(source, source.relative_to(staging).as_posix())

        _restrict_permissions(partial_archive, 0o600)
        validate_backup(partial_archive)
        os.replace(partial_archive, archive)
        published = True
        return archive
    finally:
        if not published:
            partial_archive.unlink(missing_ok=True)


def create_backup(
    destination_dir: str | os.PathLike | None = None,
    *,
    database_uri: str | None = None,
    upload_dir: str | os.PathLike | None = None,
    output_dir: str | os.PathLike | None = None,
    label: str = "manual",
) -> Path:
    """Create and verify a backup, excluding live runtime access when necessary."""
    if _touches_live_recovery_set(database_uri, upload_dir, output_dir):
        from services.runtime_lock import runtime_lock

        with runtime_lock("maintenance"):
            return _create_backup_unlocked(
                destination_dir,
                database_uri=database_uri,
                upload_dir=upload_dir,
                output_dir=output_dir,
                label=label,
            )
    return _create_backup_unlocked(
        destination_dir,
        database_uri=database_uri,
        upload_dir=upload_dir,
        output_dir=output_dir,
        label=label,
    )


def validate_backup(archive_path: str | os.PathLike) -> dict:
    """Validate paths, exact manifest membership, hashes and SQLite integrity."""
    archive = Path(archive_path).resolve()
    if not archive.is_file():
        raise FileNotFoundError(f"backup archive not found: {archive}")

    with tempfile.TemporaryDirectory(prefix="qualia_backup_validate_") as tmp:
        root = Path(tmp).resolve()
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
                target = root / Path(safe_name)
                if not _is_within(root, target):
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
        if db_name not in declared_paths:
            raise ValueError("manifest database_archive_path is not declared in files")
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


def _extract_archive(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            safe_name = _safe_member_name(info.filename)
            target = destination / safe_name
            if not _is_within(destination, target):
                raise ValueError(f"unsafe archive member: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _preserve_unvalidated_database(database_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict_permissions(backup_dir, 0o700)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"qualia_pre_restore_unvalidated_{ts}_{uuid.uuid4().hex[:8]}.db"
    shutil.copy2(database_path, target)
    _restrict_permissions(target, 0o600)
    return target


def _restore_backup_unlocked(
    archive_path: str | os.PathLike,
    *,
    apply: bool = False,
    database_uri: str | None = None,
    upload_dir: str | os.PathLike | None = None,
    output_dir: str | os.PathLike | None = None,
    backup_dir: str | os.PathLike | None = None,
    create_pre_restore_backup: bool = True,
    allow_unvalidated_pre_restore: bool = False,
) -> dict:
    source_archive = Path(archive_path).resolve()
    if not apply:
        manifest = validate_backup(source_archive)
        return {"validated": True, "restored": False, "manifest": manifest}

    database_path = _sqlite_path(database_uri)
    uploads = Path(upload_dir or config.UPLOAD_DIR).resolve()
    outputs = Path(output_dir or config.OUTPUT_DIR).resolve()
    safety_dir = Path(backup_dir or config.BACKUP_DIR).resolve()
    pre_restore_archive = None
    pre_restore_database_copy = None

    with tempfile.TemporaryDirectory(prefix="qualia_restore_stage_") as tmp:
        root = Path(tmp).resolve()
        staged_archive = root / "validated_source.zip"
        shutil.copyfile(source_archive, staged_archive)
        _restrict_permissions(staged_archive, 0o600)
        manifest = validate_backup(staged_archive)

        if create_pre_restore_backup and database_path.exists():
            try:
                pre_restore_archive = create_backup(
                    safety_dir,
                    database_uri=database_uri,
                    upload_dir=uploads,
                    output_dir=outputs,
                    label="pre_restore",
                )
            except Exception as exc:
                if not allow_unvalidated_pre_restore:
                    raise RuntimeError(
                        "pre-restore safety backup failed; rerun only with explicit "
                        "allow_unvalidated_pre_restore if recovery from a damaged DB is intended"
                    ) from exc
                pre_restore_database_copy = _preserve_unvalidated_database(
                    database_path,
                    safety_dir,
                )

        payload = root / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        _extract_archive(staged_archive, payload)

        db_name = _safe_member_name(str(manifest.get("database_archive_path") or DB_ARCHIVE_PATH))
        staged_db = payload / db_name
        if not staged_db.is_file():
            raise ValueError("staged backup database missing")

        # This is an advisory/best-effort busy check only. If an explicitly
        # acknowledged damaged DB could not be snapshotted but was preserved as a
        # raw copy, SQLite cannot reliably open it for this legacy check.
        if database_path.exists() and pre_restore_database_copy is None:
            current = sqlite3.connect(str(database_path), timeout=1)
            try:
                current.execute("BEGIN EXCLUSIVE")
                current.rollback()
            except sqlite3.OperationalError as exc:
                raise RuntimeError("database is busy; stop Qualia Transcript before restore") from exc
            finally:
                current.close()

        rollback_root = root / "rollback"
        rollback_root.mkdir(parents=True, exist_ok=True)
        rollback_db = rollback_root / "database.db"
        rollback_uploads = rollback_root / "uploads"
        rollback_outputs = rollback_root / "outputs"

        database_existed = database_path.exists()
        uploads_existed = uploads.exists()
        outputs_existed = outputs.exists()
        if database_existed:
            shutil.copy2(database_path, rollback_db)
        if uploads_existed:
            _snapshot_tree_no_links(uploads, rollback_uploads)
        if outputs_existed:
            _snapshot_tree_no_links(outputs, rollback_outputs)

        try:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staged_db, database_path)

            if uploads.exists():
                shutil.rmtree(uploads)
            if outputs.exists():
                shutil.rmtree(outputs)
            _copy_tree_from_stage(payload, "uploads", uploads)
            _copy_tree_from_stage(payload, "outputs", outputs)

            restored = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
            try:
                result = restored.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise RuntimeError(f"restored database integrity_check failed: {result}")
            finally:
                restored.close()
        except Exception:
            if database_existed and rollback_db.exists():
                shutil.copy2(rollback_db, database_path)
            elif not database_existed:
                database_path.unlink(missing_ok=True)

            if uploads.exists():
                shutil.rmtree(uploads)
            if outputs.exists():
                shutil.rmtree(outputs)
            if uploads_existed and rollback_uploads.exists():
                shutil.copytree(rollback_uploads, uploads)
            if outputs_existed and rollback_outputs.exists():
                shutil.copytree(rollback_outputs, outputs)
            raise

    return {
        "validated": True,
        "restored": True,
        "manifest": manifest,
        "pre_restore_backup": str(pre_restore_archive) if pre_restore_archive else None,
        "pre_restore_database_copy": (
            str(pre_restore_database_copy) if pre_restore_database_copy else None
        ),
    }


def restore_backup(
    archive_path: str | os.PathLike,
    *,
    apply: bool = False,
    database_uri: str | None = None,
    upload_dir: str | os.PathLike | None = None,
    output_dir: str | os.PathLike | None = None,
    backup_dir: str | os.PathLike | None = None,
    create_pre_restore_backup: bool = True,
    allow_unvalidated_pre_restore: bool = False,
) -> dict:
    """Validate a backup or apply it under the live maintenance boundary."""
    if apply and _touches_live_recovery_set(database_uri, upload_dir, output_dir):
        from services.runtime_lock import runtime_lock

        with runtime_lock("maintenance"):
            return _restore_backup_unlocked(
                archive_path,
                apply=True,
                database_uri=database_uri,
                upload_dir=upload_dir,
                output_dir=output_dir,
                backup_dir=backup_dir,
                create_pre_restore_backup=create_pre_restore_backup,
                allow_unvalidated_pre_restore=allow_unvalidated_pre_restore,
            )
    return _restore_backup_unlocked(
        archive_path,
        apply=apply,
        database_uri=database_uri,
        upload_dir=upload_dir,
        output_dir=output_dir,
        backup_dir=backup_dir,
        create_pre_restore_backup=create_pre_restore_backup,
        allow_unvalidated_pre_restore=allow_unvalidated_pre_restore,
    )
