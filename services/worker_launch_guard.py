from __future__ import annotations

from datetime import datetime, timezone

from models import db
from models.processing_job import ProcessingJob
from services.processing_jobs import launch_job_worker


_ACCEPTED_LAUNCH_STATUSES = {"pending", "running", "succeeded"}


def launch_job_or_preserve_active(
    job: ProcessingJob,
) -> tuple[int | None, Exception | None]:
    """Launch a durable worker without letting parent bookkeeping kill a live lease.

    A launch exception is authoritative only while the exact admitted job remains
    pending, on the same attempt counter, with no launch reservation/PID attached.
    Once `worker_pid` is non-null (including the 0 launch-reservation sentinel), or
    the child has advanced the job to running/succeeded, the parent must not write
    `failed`: subprocess creation may already have succeeded and the child owns the
    durable state transition.
    """
    job_id = int(job.id)
    observed_attempt = int(job.attempt_count or 0)
    observed_started_at = job.started_at

    try:
        return launch_job_worker(
            job_id,
            expected_attempt_count=observed_attempt,
            expected_started_at=observed_started_at,
        ), None
    except Exception as exc:
        db.session.rollback()

        query = (
            ProcessingJob.query
            .filter(ProcessingJob.id == job_id)
            .filter(ProcessingJob.status == "pending")
            .filter(ProcessingJob.attempt_count == observed_attempt)
            .filter(ProcessingJob.worker_pid.is_(None))
        )
        if observed_started_at is None:
            query = query.filter(ProcessingJob.started_at.is_(None))
        else:
            query = query.filter(ProcessingJob.started_at == observed_started_at)
        updated = query.update(
                {
                    ProcessingJob.status: "failed",
                    ProcessingJob.error_message: f"worker launch failed: {exc}"[:4000],
                    ProcessingJob.finished_at: datetime.now(timezone.utc),
                    ProcessingJob.worker_pid: None,
                },
                synchronize_session=False,
            )
        db.session.commit()
        db.session.expire_all()
        current = db.session.get(ProcessingJob, job_id)

        if updated == 1:
            return None, exc

        if current and current.status in _ACCEPTED_LAUNCH_STATUSES:
            current_pid = int(current.worker_pid or 0)
            return (current_pid if current_pid > 0 else None), None

        return None, exc
