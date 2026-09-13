from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

import config
from models import db
from models.processing_job import ProcessingJob
from models.project import Project
from services.job_recovery import recover_stale_jobs
from services.processing_jobs import ACTIVE_STATUSES
from services.raw_snapshot_provenance import stage_project_raw_snapshot_tombstones


class ProjectDeletionBlocked(RuntimeError):
    def __init__(self, active_job_ids: list[int]):
        self.active_job_ids = [int(value) for value in active_job_ids]
        super().__init__(
            "project has active processing jobs: "
            + ", ".join(str(value) for value in self.active_job_ids)
        )


class ProjectDeletionStorageBlocked(RuntimeError):
    """Raised when managed storage cannot be bound safely before DB deletion."""


@dataclass(frozen=True)
class ProjectStoragePlan:
    project_id: int
    interview_ids: tuple[int, ...]
    processing_job_ids: tuple[int, ...]


@dataclass(frozen=True)
class QuarantinedManagedEntry:
    original_path: Path
    quarantine_path: Path
    label: str
    entry_kind: str


@dataclass(frozen=True)
class ProjectDeletionResult:
    project_id: int
    removed_paths: int
    cleanup_errors: tuple[str, ...]


def _is_link_or_reparse(path: Path) -> bool:
    """Return True for symlinks and Windows reparse-point directories/junctions."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & reparse_flag)


def _entry_exists(path: Path) -> bool:
    """Check the directory entry itself, including broken symlinks."""
    return os.path.lexists(os.fspath(path))


def _safe_id_dir(root: Path, value: int) -> Path:
    """Return the lexical managed root/<integer-id> path without following it."""
    resolved_root = root.resolve()
    candidate = resolved_root / str(int(value))
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("managed storage path escapes root") from exc
    return candidate


def _build_storage_plan(project: Project, job_ids: list[int]) -> ProjectStoragePlan:
    interview_ids = [int(interview.id) for interview in project.interviews]
    return ProjectStoragePlan(
        project_id=int(project.id),
        interview_ids=tuple(sorted(set(interview_ids))),
        processing_job_ids=tuple(sorted(set(int(value) for value in job_ids))),
    )


def _remove_link_only(path: Path, errors: list[str]) -> int:
    """Remove only the directory entry for a link/reparse point, never its target."""
    try:
        if path.is_symlink():
            path.unlink()
        else:
            os.rmdir(path)
        return 1
    except FileNotFoundError:
        return 0
    except OSError as exc:
        errors.append(f"linked directory cleanup failed: {path}: {exc}")
        return 0


def _remove_dir(path: Path, errors: list[str]) -> int:
    try:
        if _is_link_or_reparse(path):
            return _remove_link_only(path, errors)
        if not path.exists():
            return 0
        shutil.rmtree(path)
        return 1
    except OSError as exc:
        errors.append(f"directory cleanup failed: {path}: {exc}")
        return 0


def _remove_file(path: Path, errors: list[str]) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    try:
        path.unlink()
        return 1
    except OSError as exc:
        errors.append(f"file cleanup failed: {path}: {exc}")
        return 0


def _resolve_cleanup_root(value: str | Path, label: str, errors: list[str]) -> Path | None:
    try:
        return Path(value).resolve()
    except (OSError, RuntimeError) as exc:
        errors.append(f"{label} cleanup root rejected: {exc}")
        return None


def _resolve_required_root(value: str | Path, label: str) -> Path:
    try:
        return Path(value).resolve()
    except (OSError, RuntimeError) as exc:
        raise ProjectDeletionStorageBlocked(
            f"{label} managed-storage root cannot be resolved: {exc}"
        ) from exc


def _resolve_required_logs_root() -> Path | None:
    base_root = _resolve_required_root(config.BASE_DIR, "base")
    logs_candidate = base_root / "logs"
    if not _entry_exists(logs_candidate):
        return None
    try:
        if _is_link_or_reparse(logs_candidate):
            raise ValueError("processing log directory is linked")
        logs_root = logs_candidate.resolve()
        logs_root.relative_to(base_root)
        return logs_root
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectDeletionStorageBlocked(
            f"processing log root cannot be bound safely: {exc}"
        ) from exc


def _quarantine_managed_entry(path: Path, label: str) -> QuarantinedManagedEntry | None:
    """Rename one existing managed entry inside its root without following links."""
    if not _entry_exists(path):
        return None

    try:
        is_link = _is_link_or_reparse(path)
        entry_kind = "link" if is_link else ("dir" if path.is_dir() else "file")
    except (OSError, RuntimeError) as exc:
        raise ProjectDeletionStorageBlocked(
            f"{label} managed-storage entry cannot be inspected: {path}: {exc}"
        ) from exc

    quarantine_path = path.parent / (
        f".qualia-delete-quarantine-{path.name}-{uuid4().hex}"
    )
    try:
        os.replace(path, quarantine_path)
    except OSError as exc:
        raise ProjectDeletionStorageBlocked(
            f"{label} managed-storage entry cannot be quarantined: {path}: {exc}"
        ) from exc

    return QuarantinedManagedEntry(
        original_path=path,
        quarantine_path=quarantine_path,
        label=label,
        entry_kind=entry_kind,
    )


def _restore_quarantined_entries(
    quarantined: tuple[QuarantinedManagedEntry, ...] | list[QuarantinedManagedEntry],
) -> list[str]:
    errors: list[str] = []
    for entry in reversed(tuple(quarantined)):
        if not _entry_exists(entry.quarantine_path):
            errors.append(
                f"{entry.label} quarantine entry disappeared before rollback: "
                f"{entry.quarantine_path}"
            )
            continue
        if _entry_exists(entry.original_path):
            errors.append(
                f"{entry.label} original path reappeared before rollback: "
                f"{entry.original_path}"
            )
            continue
        try:
            os.replace(entry.quarantine_path, entry.original_path)
        except OSError as exc:
            errors.append(
                f"{entry.label} quarantine rollback failed: "
                f"{entry.quarantine_path} -> {entry.original_path}: {exc}"
            )
    return errors


def _prepare_managed_storage_quarantine(
    plan: ProjectStoragePlan,
) -> tuple[QuarantinedManagedEntry, ...]:
    """Bind pre-existing ID-scoped storage to this deletion before DB commit.

    SQLite may reuse deleted highest integer primary keys. Renaming existing
    project/interview ID directories and processing-job logs to unique non-numeric
    quarantine names before commit prevents post-commit cleanup from deleting
    storage later created for reused IDs. A failed staging operation restores
    entries already moved and blocks the database deletion.
    """
    output_root = _resolve_required_root(config.OUTPUT_DIR, "output")
    upload_root = _resolve_required_root(config.UPLOAD_DIR, "upload")
    logs_root = _resolve_required_logs_root() if plan.processing_job_ids else None
    quarantined: list[QuarantinedManagedEntry] = []

    try:
        project_entry = _quarantine_managed_entry(
            _safe_id_dir(output_root, plan.project_id),
            "project output",
        )
        if project_entry is not None:
            quarantined.append(project_entry)

        for interview_id in plan.interview_ids:
            interview_entry = _quarantine_managed_entry(
                _safe_id_dir(upload_root, interview_id),
                "interview upload",
            )
            if interview_entry is not None:
                quarantined.append(interview_entry)

        if logs_root is not None:
            for job_id in plan.processing_job_ids:
                log_entry = _quarantine_managed_entry(
                    logs_root / f"processing_job_{int(job_id)}.log",
                    "processing job log",
                )
                if log_entry is not None:
                    quarantined.append(log_entry)
    except Exception as exc:
        restore_errors = _restore_quarantined_entries(quarantined)
        if restore_errors:
            raise ProjectDeletionStorageBlocked(
                "managed-storage quarantine failed and rollback was incomplete: "
                + " | ".join(restore_errors)
            ) from exc
        raise

    return tuple(quarantined)


def _remove_quarantined_entry(
    entry: QuarantinedManagedEntry,
    errors: list[str],
) -> int:
    if not _entry_exists(entry.quarantine_path):
        return 0
    if entry.entry_kind == "link":
        return _remove_link_only(entry.quarantine_path, errors)
    if entry.entry_kind == "dir":
        return _remove_dir(entry.quarantine_path, errors)
    return _remove_file(entry.quarantine_path, errors)


def _warn_if_id_path_reappeared(
    root: Path | None,
    value: int,
    label: str,
    errors: list[str],
) -> None:
    if root is None:
        return
    try:
        path = _safe_id_dir(root, value)
        if _entry_exists(path):
            errors.append(
                f"{label} path appeared after deletion staging and was left untouched: {path}"
            )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"{label} post-delete path check failed: id={int(value)}: {exc}")


def _warn_if_job_log_reappeared(
    base_root: Path | None,
    job_id: int,
    errors: list[str],
) -> None:
    if base_root is None:
        return
    logs_candidate = base_root / "logs"
    try:
        if _is_link_or_reparse(logs_candidate):
            raise ValueError("processing log directory is linked")
        logs_root = logs_candidate.resolve()
        logs_root.relative_to(base_root)
        path = logs_root / f"processing_job_{int(job_id)}.log"
        if _entry_exists(path):
            errors.append(
                f"processing job log appeared after deletion staging and was left untouched: {path}"
            )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"processing log post-delete path check failed: job_id={int(job_id)}: {exc}")


def cleanup_project_storage(
    plan: ProjectStoragePlan,
    quarantined: tuple[QuarantinedManagedEntry, ...] = (),
) -> ProjectDeletionResult:
    """Best-effort cleanup after the database deletion has committed.

    Project output/interview upload entries and captured processing-job logs that
    existed before deletion are first renamed to unique quarantine paths before the
    DB commit. Cleanup acts on those bound entries rather than reusable numeric ID
    paths. Raw transcript snapshots are deliberately excluded and require a
    separate explicit user-requested purge.
    """
    removed = 0
    errors: list[str] = []

    for entry in quarantined:
        removed += _remove_quarantined_entry(entry, errors)

    output_root = _resolve_cleanup_root(config.OUTPUT_DIR, "output", errors)
    upload_root = _resolve_cleanup_root(config.UPLOAD_DIR, "upload", errors)
    base_root = _resolve_cleanup_root(config.BASE_DIR, "base", errors)

    # Never remove reusable ID paths after the DB commit. They may already belong
    # to replacement rows created after the serialized deletion transaction ended.
    _warn_if_id_path_reappeared(
        output_root, plan.project_id, "project output", errors
    )
    if upload_root is not None:
        for interview_id in plan.interview_ids:
            _warn_if_id_path_reappeared(
                upload_root, interview_id, "interview upload", errors
            )
    for job_id in plan.processing_job_ids:
        _warn_if_job_log_reappeared(base_root, job_id, errors)

    return ProjectDeletionResult(
        project_id=plan.project_id,
        removed_paths=removed,
        cleanup_errors=tuple(errors),
    )


def _begin_project_deletion_transaction(project_id: int) -> Project:
    """Serialize project deletion with durable-job admission on SQLite."""
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("project deletion requires a clean database session")

    db.session.rollback()
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))
        current = db.session.get(Project, int(project_id))
    else:
        current = (
            Project.query
            .filter(Project.id == int(project_id))
            .with_for_update()
            .first()
        )
    if current is None:
        db.session.rollback()
        raise ValueError("project not found")
    return current


def delete_project(project: Project) -> ProjectDeletionResult:
    """Delete one project without racing durable-job admission or ID reuse."""
    project_id = int(project.id)
    recover_stale_jobs(project_id=project_id)
    quarantined: tuple[QuarantinedManagedEntry, ...] = ()

    try:
        project = _begin_project_deletion_transaction(project_id)
        active_jobs = (
            ProcessingJob.query
            .filter_by(project_id=project_id)
            .filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
            .order_by(ProcessingJob.id.asc())
            .all()
        )
        if active_jobs:
            raise ProjectDeletionBlocked([int(job.id) for job in active_jobs])

        job_ids = [
            int(row[0])
            for row in (
                db.session.query(ProcessingJob.id)
                .filter(ProcessingJob.project_id == project_id)
                .all()
            )
        ]
        plan = _build_storage_plan(project, job_ids)
        stage_project_raw_snapshot_tombstones(
            db.session,
            project_id,
            config.OUTPUT_DIR,
        )
        quarantined = _prepare_managed_storage_quarantine(plan)

        (
            ProcessingJob.query
            .filter(ProcessingJob.project_id == project_id)
            .delete(synchronize_session=False)
        )
        db.session.delete(project)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        restore_errors = _restore_quarantined_entries(quarantined)
        if restore_errors:
            raise ProjectDeletionStorageBlocked(
                "project deletion rolled back but managed-storage restoration was incomplete: "
                + " | ".join(restore_errors)
            ) from exc
        raise

    return cleanup_project_storage(plan, quarantined)
