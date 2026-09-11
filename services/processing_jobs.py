from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

import config
from models import db
from models.processing_job import ProcessingJob


ACTIVE_STATUSES = {"pending", "running"}
JOB_TYPES = {"transcribe", "map", "analyze", "project_pipeline"}
_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]+")
_LAUNCH_RESERVED_PID = 0


class JobLeaseLost(RuntimeError):
    """The worker attempt no longer owns the durable job row."""


def _utcnow():
    return datetime.now(timezone.utc)


def _json_dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _safe_error(exc: Exception) -> str:
    return _KEY_RE.sub("[REDACTED_KEY]", str(exc or ""))[:4000]


def create_or_get_active_job(project_id: int, job_type: str, interview_id: int | None = None):
    if job_type not in JOB_TYPES:
        raise ValueError(f"unsupported job_type: {job_type}")

    query = ProcessingJob.query.filter_by(
        project_id=project_id,
        interview_id=interview_id,
        job_type=job_type,
    ).filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
    existing = query.order_by(ProcessingJob.id.desc()).first()
    if existing:
        return existing, False

    job = ProcessingJob(
        project_id=project_id,
        interview_id=interview_id,
        job_type=job_type,
        status="pending",
        progress_json=_json_dump({"stage": "queued"}),
    )
    db.session.add(job)
    db.session.commit()
    return job, True


def _attempt_number(job: ProcessingJob) -> int:
    frozen = getattr(job, "_lease_attempt", None)
    attempt = int(frozen if frozen is not None else (job.attempt_count or 0))
    if attempt <= 0:
        raise JobLeaseLost(f"processing job has no active attempt: job_id={job.id}")
    return attempt


def _owned_running_query(job_id: int, attempt_count: int):
    return (
        ProcessingJob.query
        .filter(ProcessingJob.id == int(job_id))
        .filter(ProcessingJob.status == "running")
        .filter(ProcessingJob.attempt_count == int(attempt_count))
    )


def assert_job_lease(job: ProcessingJob) -> ProcessingJob:
    """Verify that this worker attempt still owns the durable job row."""
    job_id = int(job.id)
    attempt_count = _attempt_number(job)
    db.session.expire_all()
    current = db.session.get(ProcessingJob, job_id)
    if (
        not current
        or current.status != "running"
        or int(current.attempt_count or 0) != attempt_count
    ):
        raise JobLeaseLost(
            f"processing job lease lost: job_id={job_id} attempt={attempt_count}"
        )
    return current


def begin_job_result_write(job: ProcessingJob) -> ProcessingJob:
    """Acquire a DB write reservation and validate the immutable attempt token.

    Called after expensive external work and before canonical result rows change.
    SQLite uses BEGIN IMMEDIATE so stale recovery/retry cannot pass between this
    lease check and the caller's result commit. Other databases use a row lock.
    """
    job_id = int(job.id)
    attempt_count = _attempt_number(job)
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("result write guard requires a clean database session")

    db.session.rollback()
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))
        current = db.session.get(ProcessingJob, job_id)
    else:
        current = (
            ProcessingJob.query
            .filter(ProcessingJob.id == job_id)
            .with_for_update()
            .first()
        )

    if (
        not current
        or current.status != "running"
        or int(current.attempt_count or 0) != attempt_count
    ):
        db.session.rollback()
        raise JobLeaseLost(
            f"processing job result lease lost: job_id={job_id} attempt={attempt_count}"
        )
    return current


def update_progress(job: ProcessingJob, stage: str, **details) -> None:
    """Persist progress only while this worker attempt still owns the job."""
    job_id = int(job.id)
    attempt_count = _attempt_number(job)
    payload = {"stage": stage, **details}
    updated = _owned_running_query(job_id, attempt_count).update(
        {ProcessingJob.progress_json: _json_dump(payload)},
        synchronize_session=False,
    )
    db.session.commit()
    db.session.expire_all()
    if updated != 1:
        raise JobLeaseLost(
            f"processing job lease lost: job_id={job_id} attempt={attempt_count}"
        )


def _refresh_job(job_id: int) -> ProcessingJob:
    db.session.expire_all()
    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError("processing job not found")
    return job


def _reserve_worker_launch(job_id: int) -> tuple[ProcessingJob, bool]:
    reserved = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .filter(ProcessingJob.worker_pid.is_(None))
        .update(
            {
                ProcessingJob.worker_pid: _LAUNCH_RESERVED_PID,
                ProcessingJob.progress_json: _json_dump({"stage": "launching"}),
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    return _refresh_job(job_id), reserved == 1


def _release_worker_launch_reservation(job_id: int) -> None:
    (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
        .filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
        .update(
            {
                ProcessingJob.worker_pid: None,
                ProcessingJob.progress_json: _json_dump({"stage": "queued"}),
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    db.session.expire_all()


def _record_worker_launch(job_id: int, pid: int) -> bool:
    pid = int(pid)
    pending_updated = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
        .update(
            {
                ProcessingJob.worker_pid: pid,
                ProcessingJob.progress_json: _json_dump({"stage": "worker_started", "pid": pid}),
            },
            synchronize_session=False,
        )
    )
    running_updated = 0
    if not pending_updated:
        running_updated = (
            ProcessingJob.query
            .filter(ProcessingJob.id == job_id)
            .filter(ProcessingJob.status == "running")
            .filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
            .update(
                {ProcessingJob.worker_pid: pid},
                synchronize_session=False,
            )
        )
    db.session.commit()
    db.session.expire_all()
    return bool(pending_updated or running_updated)


def launch_job_worker(job_id: int) -> int:
    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError("processing job not found")
    if job.status not in ACTIVE_STATUSES:
        raise ValueError(f"job cannot be launched from status={job.status}")

    root = Path(config.BASE_DIR).resolve()
    script = root / "scripts" / "run_processing_job.py"
    if not script.is_file():
        raise FileNotFoundError(f"worker script not found: {script}")

    job, reserved = _reserve_worker_launch(job_id)
    if not reserved:
        if job.status in ACTIVE_STATUSES:
            return int(job.worker_pid or 0)
        raise ValueError(f"job cannot be launched from status={job.status}")

    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"processing_job_{job.id}.log"

    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    kwargs = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
        "env": child_env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        with log_path.open("ab", buffering=0) as log_handle:
            process = subprocess.Popen(
                [sys.executable, str(script), "--job-id", str(job.id)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
    except Exception:
        _release_worker_launch_reservation(job_id)
        raise

    _record_worker_launch(job_id, process.pid)
    current = _refresh_job(job_id)
    if current.worker_pid and int(current.worker_pid) > 0:
        return int(current.worker_pid)
    return int(process.pid)


def retry_failed_job(job: ProcessingJob) -> ProcessingJob:
    job_id = int(job.id)
    retried = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "failed")
        .update(
            {
                ProcessingJob.status: "pending",
                ProcessingJob.progress_json: _json_dump({"stage": "retry_queued"}),
                ProcessingJob.result_json: None,
                ProcessingJob.error_message: None,
                ProcessingJob.worker_pid: None,
                ProcessingJob.started_at: None,
                ProcessingJob.finished_at: None,
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    current = _refresh_job(job_id)
    if retried != 1:
        raise ValueError("only failed jobs can be retried")
    return current


def _claim_pending_job(job_id: int, worker_pid: int | None = None) -> tuple[ProcessingJob, bool]:
    values = {
        ProcessingJob.status: "running",
        ProcessingJob.started_at: _utcnow(),
        ProcessingJob.finished_at: None,
        ProcessingJob.attempt_count: ProcessingJob.attempt_count + 1,
        ProcessingJob.error_message: None,
    }
    if worker_pid is not None and int(worker_pid) > 0:
        values[ProcessingJob.worker_pid] = int(worker_pid)

    claimed = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .update(values, synchronize_session=False)
    )
    db.session.commit()
    current = _refresh_job(job_id)
    if claimed == 1:
        current._lease_attempt = int(current.attempt_count or 0)
    return current, claimed == 1


def _finish_job_success(job_id: int, attempt_count: int, result) -> tuple[ProcessingJob, bool]:
    updated = _owned_running_query(job_id, attempt_count).update(
        {
            ProcessingJob.status: "succeeded",
            ProcessingJob.result_json: _json_dump(result or {}),
            ProcessingJob.progress_json: _json_dump({"stage": "completed"}),
            ProcessingJob.error_message: None,
            ProcessingJob.finished_at: _utcnow(),
            ProcessingJob.worker_pid: None,
        },
        synchronize_session=False,
    )
    db.session.commit()
    return _refresh_job(job_id), updated == 1


def _finish_job_failure(job_id: int, attempt_count: int, exc: Exception) -> tuple[ProcessingJob, bool]:
    partial_result = getattr(exc, "job_result", None)
    values = {
        ProcessingJob.status: "failed",
        ProcessingJob.error_message: _safe_error(exc),
        ProcessingJob.finished_at: _utcnow(),
        ProcessingJob.worker_pid: None,
    }
    if partial_result is not None:
        values[ProcessingJob.result_json] = _json_dump(partial_result)
        values[ProcessingJob.progress_json] = _json_dump({
            "stage": "failed_partial",
            "failed_interview_count": partial_result.get("failed_interview_count", 0),
            "interview_count": partial_result.get("interview_count", 0),
        })
    else:
        values[ProcessingJob.progress_json] = _json_dump({"stage": "failed"})

    updated = _owned_running_query(job_id, attempt_count).update(
        values,
        synchronize_session=False,
    )
    db.session.commit()
    return _refresh_job(job_id), updated == 1


def _perform_transcription(job: ProcessingJob) -> dict:
    from models.interview import Interview, Transcription
    from services.processing_result_guard import discard_incomplete_transcription_segments
    from services.transcription import get_default_transcription_model
    from services.transcription_dispatch import run_transcription

    interview = db.session.get(Interview, job.interview_id)
    if not interview or interview.project_id != job.project_id:
        raise ValueError("interview not found in job project")
    if not interview.media_files:
        raise ValueError("音声ファイルが登録されていません")

    media = interview.media_files[-1]
    existing = (
        Transcription.query
        .filter_by(media_file_id=media.id, status="done")
        .order_by(Transcription.id.desc())
        .first()
    )
    if existing:
        return {
            "transcription_id": existing.id,
            "already_done": True,
            "segment_count": len(interview.segments),
        }

    update_progress(job, "transcribing")

    # Cleanup is a destructive canonical write. Fence it so an old transcribe
    # worker cannot delete partial rows that now belong to a newer retry.
    begin_job_result_write(job)
    cleanup = discard_incomplete_transcription_segments(media.id)

    # Cleanup commits and releases the reservation. Revalidate the immutable
    # attempt token before creating the next Transcription attempt.
    begin_job_result_write(job)
    tr = Transcription(
        media_file_id=media.id,
        whisper_model=get_default_transcription_model(),
        language="ja",
        status="pending",
    )
    db.session.add(tr)
    db.session.commit()
    update_progress(job, "transcribing", transcription_id=tr.id)
    result = run_transcription(
        tr.id,
        lease_check=lambda: assert_job_lease(job),
    )
    return {
        "transcription_id": tr.id,
        "discarded_partial_segment_count": cleanup["deleted_segment_count"],
        **result,
    }


def _perform_mapping(job: ProcessingJob) -> dict:
    from models.interview import Interview
    from services.mapper import run_mapping
    from services.processing_result_guard import find_completed_mapping_count_for_job

    interview = db.session.get(Interview, job.interview_id)
    if not interview or interview.project_id != job.project_id:
        raise ValueError("interview not found in job project")

    existing_count = find_completed_mapping_count_for_job(job)
    if existing_count is not None:
        update_progress(job, "mapping", mapped_count=existing_count, already_done=True)
        return {"mapped_count": existing_count, "already_done": True}

    update_progress(job, "mapping")
    count = run_mapping(
        interview.id,
        result_write_guard=lambda: begin_job_result_write(job),
    )
    return {"mapped_count": count}


def _perform_analysis(job: ProcessingJob) -> dict:
    from models.interview import Interview
    from services.analyzer import analyze_interview_summary
    from services.processing_result_guard import find_completed_analysis_for_job

    interview = db.session.get(Interview, job.interview_id)
    if not interview or interview.project_id != job.project_id:
        raise ValueError("interview not found in job project")

    existing = find_completed_analysis_for_job(job)
    if existing:
        update_progress(job, "analyzing", analysis_id=existing.id, already_done=True)
        return {"analysis_id": existing.id, "already_done": True}

    update_progress(job, "analyzing")
    analysis = analyze_interview_summary(
        interview.id,
        result_write_guard=lambda: begin_job_result_write(job),
    )
    return {"analysis_id": analysis.id}


def _perform_project_pipeline(job: ProcessingJob) -> dict:
    from services.project_pipeline import run_project_pipeline

    return run_project_pipeline(job, update_progress)


def execute_job(
    job_id: int,
    handlers: dict[str, object] | None = None,
    *,
    worker_pid: int | None = None,
) -> ProcessingJob:
    job, claimed = _claim_pending_job(job_id, worker_pid=worker_pid)
    if not claimed:
        return job
    attempt_count = _attempt_number(job)

    default_handlers = {
        "transcribe": _perform_transcription,
        "map": _perform_mapping,
        "analyze": _perform_analysis,
        "project_pipeline": _perform_project_pipeline,
    }
    selected = handlers or default_handlers

    try:
        handler = selected.get(job.job_type)
        if handler is None:
            raise ValueError(f"no handler for job_type={job.job_type}")

        result = handler(job)
        current, _ = _finish_job_success(job_id, attempt_count, result)
        return current
    except Exception as exc:
        db.session.rollback()
        current, _ = _finish_job_failure(job_id, attempt_count, exc)
        return current
