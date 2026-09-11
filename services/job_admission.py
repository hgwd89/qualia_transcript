from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from models import db
from models.processing_job import ProcessingJob
from services.job_recovery import recover_stale_jobs
from services.processing_jobs import ACTIVE_STATUSES, JOB_TYPES, _json_dump


PROJECT_EXCLUSIVE_JOB_TYPES = {"project_pipeline", "analyze_cross", "analyze_integrated"}


@dataclass(frozen=True)
class JobAdmission:
    job_id: int | None = None
    created: bool = False
    conflict_job_id: int | None = None
    error: str | None = None


def _require_clean_session() -> None:
    """Admission may discard read transactions, never pending application writes."""
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("processing job admission requires a clean database session")


def _begin_immediate() -> None:
    # Request handlers typically performed validation reads first. End that read
    # transaction, then acquire SQLite's write reservation before inspecting
    # active jobs so another admission cannot pass the same check concurrently.
    _require_clean_session()
    db.session.rollback()
    db.session.execute(text("BEGIN IMMEDIATE"))


def _same_scope(
    job: ProcessingJob,
    job_type: str,
    interview_id: int | None,
    question_id: int | None,
) -> bool:
    return (
        job.job_type == job_type
        and job.interview_id == interview_id
        and job.question_id == question_id
    )


def _conflicts(
    job: ProcessingJob,
    job_type: str,
    interview_id: int | None,
    question_id: int | None,
) -> bool:
    if _same_scope(job, job_type, interview_id, question_id):
        return False
    if job_type in PROJECT_EXCLUSIVE_JOB_TYPES or job.job_type in PROJECT_EXCLUSIVE_JOB_TYPES:
        return True
    return interview_id is not None and job.interview_id == interview_id


def _active_jobs(project_id: int) -> list[ProcessingJob]:
    return (
        ProcessingJob.query
        .filter_by(project_id=project_id)
        .filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
        .order_by(ProcessingJob.id.desc())
        .all()
    )


def admit_processing_job(
    project_id: int,
    job_type: str,
    interview_id: int | None = None,
    *,
    question_id: int | None = None,
) -> JobAdmission:
    """Atomically reuse, reject, or create one processing job.

    SQLite `BEGIN IMMEDIATE` serializes all callers of this admission path before
    they inspect the active-job set. The decision and optional INSERT therefore
    happen in one database transaction rather than a check-then-create race.
    """
    if job_type not in JOB_TYPES:
        raise ValueError(f"unsupported job_type: {job_type}")

    recover_stale_jobs(project_id=project_id)
    try:
        _begin_immediate()
        active = _active_jobs(project_id)

        for job in active:
            if _same_scope(job, job_type, interview_id, question_id):
                db.session.commit()
                return JobAdmission(job_id=job.id, created=False)

        for job in active:
            if _conflicts(job, job_type, interview_id, question_id):
                db.session.commit()
                return JobAdmission(conflict_job_id=job.id)

        job = ProcessingJob(
            project_id=project_id,
            interview_id=interview_id,
            question_id=question_id,
            job_type=job_type,
            status="pending",
            progress_json=_json_dump({"stage": "queued"}),
        )
        db.session.add(job)
        db.session.flush()
        job_id = int(job.id)
        db.session.commit()
        return JobAdmission(job_id=job_id, created=True)
    except Exception:
        db.session.rollback()
        raise


def admit_retry_job(job_id: int) -> JobAdmission:
    """Atomically validate conflicts and requeue exactly one failed job."""
    target = db.session.get(ProcessingJob, job_id)
    if not target:
        return JobAdmission(error="processing job not found")
    project_id = int(target.project_id)

    recover_stale_jobs(project_id=project_id)
    try:
        _begin_immediate()
        target = db.session.get(ProcessingJob, job_id)
        if not target:
            db.session.commit()
            return JobAdmission(error="processing job not found")
        if target.status != "failed":
            db.session.commit()
            return JobAdmission(job_id=target.id, error="only failed jobs can be retried")

        active = _active_jobs(project_id)
        for job in active:
            if _conflicts(
                job,
                target.job_type,
                target.interview_id,
                target.question_id,
            ) or _same_scope(
                job,
                target.job_type,
                target.interview_id,
                target.question_id,
            ):
                db.session.commit()
                return JobAdmission(job_id=target.id, conflict_job_id=job.id)

        target.status = "pending"
        target.progress_json = _json_dump({"stage": "retry_queued"})
        target.result_json = None
        target.error_message = None
        target.worker_pid = None
        target.started_at = None
        target.finished_at = None
        db.session.commit()
        return JobAdmission(job_id=target.id, created=True)
    except Exception:
        db.session.rollback()
        raise
