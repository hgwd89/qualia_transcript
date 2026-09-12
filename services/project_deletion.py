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
    transcription_ids: tuple[int, ...]
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
    root = root.resolve()
    candidate = root / str(int(value))
    if _is_link_or_reparse(candidate):
        raise ValueError("managed ID directory is a symlink or reparse point")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("managed storage path escapes root") from exc
    # Return the original root/id path, not the resolved target. This prevents a
    # linked ID directory from being converted into a recursive-delete target.
    return candidate


def _build_storage_plan(project: Project, job_ids: list[int]) -> ProjectStoragePlan:
    interview_ids: list[int] = []
    transcription_ids: list[int] = []
    for interview in project.interviews:
        interview_ids.append(int(interview.id))
        for media in interview.media_files:
            for transcription in media.transcriptions:
                transcription_ids.append(int(transcription.id))

    return ProjectStoragePlan(
        project_id=int(project.id),
        interview_ids=tuple(sorted(set(interview_ids))),
        transcription_ids=tuple(sorted(set(transcription_ids))),
        processing_job_ids=tuple(sorted(set(int(value) for value in job_ids))),
    )


def _remove_dir(path: Path, errors: list[str]) -> int:
    try:
        if _is_link_or_reparse(path):
            errors.append(f"directory cleanup rejected linked path: {path}")
            return 0
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

    DB deletion is committed first so a filesystem error never leaves database
    rows pointing at media/output files that were already removed. A hard process
    crash after DB commit can still leave orphan files; integrity audits can safely
    remove those because all paths below are project/interview/job scoped.
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

    if output_root is not None:
        raw_candidate = output_root / "raw_transcripts"
        try:
            if _is_link_or_reparse(raw_candidate):
                raise ValueError("raw transcript directory is linked")
            raw_root = raw_candidate.resolve()
            raw_root.relative_to(output_root)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append(f"raw transcript cleanup path rejected: {exc}")
        else:
            if raw_root.is_dir():
                for transcription_id in plan.transcription_ids:
                    for path in raw_root.glob(f"transcription_{int(transcription_id)}_*.json"):
                        if path.is_file() or path.is_symlink():
                            removed += _remove_file(path, errors)

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

    # Validation/recovery reads may already have opened a transaction. End it,
    # then acquire the same SQLite write reservation used by job admission before
    # checking active jobs and deleting any rows.
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
    """Delete one project without racing durable-job admission or workers.

    Stale jobs are recovered first. Deletion then acquires the SQLite write
    reservation used by job admission before reloading the project and checking
    active jobs. The active-job decision and database deletion therefore remain in
    one serialized transaction. Filesystem cleanup occurs only after DB commit.
    """
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
