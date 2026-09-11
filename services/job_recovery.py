from __future__ import annotations

from datetime import datetime, timedelta, timezone

from models import db
from models.processing_job import ProcessingJob


ACTIVE_STATUSES = ("pending", "running")
ACTIVE_WITHOUT_WORKER_GRACE = timedelta(minutes=5)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def stale_reason(job: ProcessingJob, now: datetime | None = None) -> str | None:
    """Return an automatic-recovery reason only when recovery is unambiguous.

    A PID-backed job is never failed merely because it is old: without a
    trustworthy liveness/identity signal that could race a legitimately long
    operation and permit duplicate writes. Such jobs require explicit operator
    recovery after confirming the worker has stopped.
    """
    if job.status not in ACTIVE_STATUSES:
        return None
    current = _utc(now) or datetime.now(timezone.utc)
    created = _utc(job.created_at)

    if not job.worker_pid and created:
        if current - created > ACTIVE_WITHOUT_WORKER_GRACE:
            return "active job has no worker after launch grace period"
    return None


def mark_job_failed(job: ProcessingJob, reason: str) -> ProcessingJob:
    job.status = "failed"
    job.error_message = f"job recovery: {reason}"[:4000]
    job.progress_json = '{"stage":"recovered_failed"}'
    job.finished_at = datetime.now(timezone.utc)
    job.worker_pid = None
    db.session.add(job)
    db.session.commit()
    return job


def recover_stale_jobs(project_id: int | None = None) -> list[ProcessingJob]:
    query = ProcessingJob.query.filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
    if project_id is not None:
        query = query.filter(ProcessingJob.project_id == project_id)

    recovered = []
    for job in query.order_by(ProcessingJob.id.asc()).all():
        reason = stale_reason(job)
        if reason:
            mark_job_failed(job, reason)
            recovered.append(job)
    return recovered
