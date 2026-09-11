from __future__ import annotations

import os
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

    No-PID jobs get a startup grace period. PID-backed jobs are recovered only
    when the OS confirms that the worker process is no longer alive. A live or
    inconclusive PID is never released automatically.
    """
    if job.status not in ACTIVE_STATUSES:
        return None

    current = _utc(now) or datetime.now(timezone.utc)
    created = _utc(job.created_at)

    if not job.worker_pid:
        if created and current - created > ACTIVE_WITHOUT_WORKER_GRACE:
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


def mark_job_failed(job: ProcessingJob, reason: str) -> ProcessingJob:
    job.status = "failed"
    job.error_message = f"job recovery: {reason}"[:4000]
    job.progress_json = '{"stage":"recovered_failed"}'
    job.finished_at = datetime.now(timezone.utc)
    job.worker_pid = None
    db.session.add(job)
    db.session.commit()
    return job


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
        if reason:
            mark_job_failed(job, reason)
            recovered.append(job)
    return recovered
