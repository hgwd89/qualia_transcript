from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text

import config
from models import db
from models.processing_job import ProcessingJob
from models.project import Project
from services.job_recovery import recover_stale_jobs
from services.processing_jobs import ACTIVE_STATUSES


class ProjectDeletionBlocked(RuntimeError):
    def __init__(self, active_job_ids: list[int]):
        self.active_job_ids = [int(value) for value in active_job_ids]
        super().__init__(
            "project has active processing jobs: "
            + ", ".join(str(value) for value in self.active_job_ids)
        )


@dataclass(frozen=True)
class ProjectStoragePlan:
    project_id: int
    interview_ids: tuple[int, ...]
    processing_job_ids: tuple[int, ...]


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


def _remove_safe_id_dir(root: Path, value: int, label: str, errors: list[str]) -> int:
    try:
        path = _safe_id_dir(root, value)
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(f"{label} cleanup path rejected: id={int(value)}: {exc}")
        return 0
    return _remove_dir(path, errors)


def _resolve_cleanup_root(value: str | Path, label: str, errors: list[str]) -> Path | None:
    try:
        return Path(value).resolve()
    except (OSError, RuntimeError) as exc:
        errors.append(f"{label} cleanup root rejected: {exc}")
        return None


def cleanup_project_storage(plan: ProjectStoragePlan) -> ProjectDeletionResult:
    """Best-effort cleanup after the database deletion has committed.

    Project-scoped generated outputs, interview upload directories, and processing
    job logs are cleaned after commit. Raw transcript snapshots are deliberately
    excluded: repository policy treats them as high-sensitivity source snapshots
    that must not be deleted unless the user explicitly requests that operation.
    """
    removed = 0
    errors: list[str] = []

    output_root = _resolve_cleanup_root(config.OUTPUT_DIR, "output", errors)
    upload_root = _resolve_cleanup_root(config.UPLOAD_DIR, "upload", errors)
    base_root = _resolve_cleanup_root(config.BASE_DIR, "base", errors)

    if output_root is not None:
        removed += _remove_safe_id_dir(
            output_root, plan.project_id, "project output", errors
        )
    if upload_root is not None:
        for interview_id in plan.interview_ids:
            removed += _remove_safe_id_dir(
                upload_root, interview_id, "interview upload", errors
            )

    if base_root is not None:
        logs_candidate = base_root / "logs"
        try:
            if _is_link_or_reparse(logs_candidate):
                raise ValueError("processing log directory is linked")
            logs_root = logs_candidate.resolve()
            logs_root.relative_to(base_root)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"processing log cleanup path rejected: {exc}")
        else:
            for job_id in plan.processing_job_ids:
                removed += _remove_file(
                    logs_root / f"processing_job_{int(job_id)}.log", errors
                )

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
    """Delete one project without racing durable-job admission or workers."""
    project_id = int(project.id)
    recover_stale_jobs(project_id=project_id)

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

        (
            ProcessingJob.query
            .filter(ProcessingJob.project_id == project_id)
            .delete(synchronize_session=False)
        )
        db.session.delete(project)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    return cleanup_project_storage(plan)
