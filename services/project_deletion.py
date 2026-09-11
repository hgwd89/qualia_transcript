from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

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


def _safe_id_dir(root: Path, value: int) -> Path:
    root = root.resolve()
    candidate = (root / str(int(value))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("managed storage path escapes root") from exc
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
    if not path.exists():
        return 0
    try:
        shutil.rmtree(path)
        return 1
    except OSError as exc:
        errors.append(f"directory cleanup failed: {path}: {exc}")
        return 0


def _remove_file(path: Path, errors: list[str]) -> int:
    if not path.exists():
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
    except (OSError, ValueError) as exc:
        errors.append(f"{label} cleanup path rejected: id={int(value)}: {exc}")
        return 0
    return _remove_dir(path, errors)


def cleanup_project_storage(plan: ProjectStoragePlan) -> ProjectDeletionResult:
    """Best-effort cleanup after the database deletion has committed.

    DB deletion is committed first so a filesystem error never leaves database
    rows pointing at media/output files that were already removed. A hard process
    crash after DB commit can still leave orphan files; integrity audits can safely
    remove those because all paths below are project/interview/job scoped.
    """
    removed = 0
    errors: list[str] = []

    output_root = Path(config.OUTPUT_DIR).resolve()
    upload_root = Path(config.UPLOAD_DIR).resolve()
    base_root = Path(config.BASE_DIR).resolve()

    removed += _remove_safe_id_dir(
        output_root, plan.project_id, "project output", errors
    )
    for interview_id in plan.interview_ids:
        removed += _remove_safe_id_dir(
            upload_root, interview_id, "interview upload", errors
        )

    raw_root = (output_root / "raw_transcripts").resolve()
    try:
        raw_root.relative_to(output_root)
    except ValueError:
        errors.append("raw transcript cleanup path rejected: escapes OUTPUT_DIR")
    else:
        if raw_root.is_dir():
            for transcription_id in plan.transcription_ids:
                for path in raw_root.glob(f"transcription_{int(transcription_id)}_*.json"):
                    if path.is_file() or path.is_symlink():
                        removed += _remove_file(path, errors)

    logs_root = (base_root / "logs").resolve()
    try:
        logs_root.relative_to(base_root)
    except ValueError:
        errors.append("processing log cleanup path rejected: escapes BASE_DIR")
    else:
        for job_id in plan.processing_job_ids:
            removed += _remove_file(logs_root / f"processing_job_{int(job_id)}.log", errors)

    return ProjectDeletionResult(
        project_id=plan.project_id,
        removed_paths=removed,
        cleanup_errors=tuple(errors),
    )


def delete_project(project: Project) -> ProjectDeletionResult:
    """Delete one project without racing an active durable worker.

    Stale jobs are recovered first. Any still-active job blocks deletion. Terminal
    ProcessingJob rows are explicitly removed because Project does not own them via
    an ORM delete-orphan relationship. Filesystem cleanup occurs only after the DB
    transaction commits.
    """
    project_id = int(project.id)
    recover_stale_jobs(project_id=project_id)

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

    try:
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
