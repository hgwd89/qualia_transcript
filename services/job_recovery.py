from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from models import db
from models.processing_job import ProcessingJob
from services.processing_result_guard import find_completed_result_for_active_job


ACTIVE_STATUSES = ("pending", "running")
ACTIVE_WITHOUT_WORKER_GRACE = timedelta(minutes=5)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def worker_pid_liveness(pid: int) -> bool | None:
    """Return True/False only when process liveness can be determined safely.

    None means the OS query was inconclusive. Automatic recovery must never
    treat an inconclusive result as a dead worker.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            ERROR_INVALID_PARAMETER = 87
            ERROR_NOT_FOUND = 1168

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE

            get_exit_code = kernel32.GetExitCodeProcess
            get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            get_exit_code.restype = wintypes.BOOL

            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL

            handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                error = ctypes.get_last_error()
                if error in {ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND}:
                    return False
                return None

            try:
                exit_code = wintypes.DWORD()
                if not get_exit_code(handle, ctypes.byref(exit_code)):
                    return None
                return int(exit_code.value) == STILL_ACTIVE
            finally:
                close_handle(handle)
        except Exception:
            return None

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def stale_reason(
    job: ProcessingJob,
    now: datetime | None = None,
    pid_checker=None,
) -> str | None:
    """Return an automatic-recovery reason only when recovery is unambiguous.

    No-PID jobs get a startup grace period for the *current active attempt*.
    `started_at` is that attempt anchor when present (retry admission records it,
    and worker claim refreshes it); first-time pending jobs fall back to their
    creation time. PID-backed jobs are recovered only when the OS confirms that
    the worker process is no longer alive. A live or inconclusive PID is never
    released automatically.
    """
    if job.status not in ACTIVE_STATUSES:
        return None

    current = _utc(now) or datetime.now(timezone.utc)
    active_since = _utc(job.started_at) or _utc(job.created_at)

    if not job.worker_pid:
        if active_since and current - active_since > ACTIVE_WITHOUT_WORKER_GRACE:
            return "active job has no worker after launch grace period"
        return None

    checker = pid_checker or worker_pid_liveness
    try:
        alive = checker(int(job.worker_pid))
    except Exception:
        alive = None
    if alive is False:
        return "worker process is no longer running"
    return None


def _unchanged_active_query(job: ProcessingJob):
    """Return a CAS query for the exact active state recovery inspected."""
    query = (
        ProcessingJob.query
        .filter(ProcessingJob.id == int(job.id))
        .filter(ProcessingJob.status == str(job.status))
        .filter(ProcessingJob.attempt_count == int(job.attempt_count or 0))
    )
    if job.worker_pid is None:
        query = query.filter(ProcessingJob.worker_pid.is_(None))
    else:
        query = query.filter(ProcessingJob.worker_pid == int(job.worker_pid))
    return query


def _mark_job_failed_if_unchanged(
    job: ProcessingJob,
    reason: str,
) -> tuple[ProcessingJob, bool]:
    """Fail only the exact active job state that recovery inspected.

    Recovery performs an OS liveness check outside the database transaction.
    During that gap the worker may finish, a launcher may attach a PID, or the
    job may be retried and claimed by a newer attempt. Treat the observed
    status/attempt/PID as a compare-and-swap token so an old recovery decision
    cannot overwrite newer durable state.
    """
    if str(job.status) not in ACTIVE_STATUSES:
        db.session.expire_all()
        current = db.session.get(ProcessingJob, int(job.id))
        return (current or job), False

    updated = _unchanged_active_query(job).update(
        {
            ProcessingJob.status: "failed",
            ProcessingJob.error_message: f"job recovery: {reason}"[:4000],
            ProcessingJob.progress_json: '{"stage":"recovered_failed"}',
            ProcessingJob.finished_at: datetime.now(timezone.utc),
            ProcessingJob.worker_pid: None,
        },
        synchronize_session=False,
    )
    db.session.commit()
    db.session.expire_all()
    current = db.session.get(ProcessingJob, int(job.id))
    return (current or job), updated == 1


def _mark_job_succeeded_if_unchanged(
    job: ProcessingJob,
    result: dict,
) -> tuple[ProcessingJob, bool]:
    """Recover a dead worker whose current attempt already committed its result."""
    if str(job.status) != "running":
        db.session.expire_all()
        current = db.session.get(ProcessingJob, int(job.id))
        return (current or job), False

    updated = _unchanged_active_query(job).update(
        {
            ProcessingJob.status: "succeeded",
            ProcessingJob.result_json: json.dumps(result, ensure_ascii=False, default=str),
            ProcessingJob.progress_json: '{"stage":"recovered_completed"}',
            ProcessingJob.error_message: None,
            ProcessingJob.finished_at: datetime.now(timezone.utc),
            ProcessingJob.worker_pid: None,
        },
        synchronize_session=False,
    )
    db.session.commit()
    db.session.expire_all()
    current = db.session.get(ProcessingJob, int(job.id))
    return (current or job), updated == 1


def mark_job_failed(job: ProcessingJob, reason: str) -> ProcessingJob:
    """Compatibility wrapper for callers that only need the current job row."""
    current, _ = _mark_job_failed_if_unchanged(job, reason)
    return current


def recover_stale_jobs(
    project_id: int | None = None,
    *,
    pid_checker=None,
) -> list[ProcessingJob]:
    query = ProcessingJob.query.filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
    if project_id is not None:
        query = query.filter(ProcessingJob.project_id == project_id)

    recovered = []
    for job in query.order_by(ProcessingJob.id.asc()).all():
        reason = stale_reason(job, pid_checker=pid_checker)
        if not reason:
            continue

        committed_result = find_completed_result_for_active_job(job)
        if committed_result is not None:
            current, changed = _mark_job_succeeded_if_unchanged(job, committed_result)
        else:
            current, changed = _mark_job_failed_if_unchanged(job, reason)
        if changed:
            recovered.append(current)
    return recovered
