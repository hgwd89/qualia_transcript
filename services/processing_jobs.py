from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
from models import db
from models.processing_job import ProcessingJob


ACTIVE_STATUSES = {"pending", "running"}
JOB_TYPES = {"transcribe", "map", "analyze", "project_pipeline"}
_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]+")
_LAUNCH_RESERVED_PID = 0


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


def update_progress(job: ProcessingJob, stage: str, **details) -> None:
    payload = {"stage": stage, **details}
    job.progress_json = _json_dump(payload)
    db.session.add(job)
    db.session.commit()


def _refresh_job(job_id: int) -> ProcessingJob:
    db.session.expire_all()
    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError("processing job not found")
    return job


def _reserve_worker_launch(job_id: int) -> tuple[ProcessingJob, bool]:
    """Reserve the right to spawn a worker exactly once for a pending job.

    worker_pid=0 is a short-lived durable sentinel meaning "launcher owns the
    spawn slot but the OS PID has not been persisted yet". Existing recovery
    treats 0 as no PID and therefore still gives the normal five-minute grace.
    A running job is never eligible for a new spawn reservation.
    """
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
    """Release only this launcher's unmaterialized reservation after Popen fails."""
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
    """Persist a spawned PID without resurrecting or overwriting a terminal job.

    If the child has already atomically claimed the job, it writes its own PID.
    The parent only fills the PID while the launch-reservation sentinel remains.
    """
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
    """Launch one detached worker process and durably associate its PID."""
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
        # Another launcher already owns the pending spawn slot, the worker has
        # already claimed the job, or the job has completed. Never spawn again.
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
    """Atomically transition pending -> running.

    The conditional UPDATE is the execution lease: if multiple workers reach
    this function, exactly one can change the row from pending to running. All
    others observe rowcount=0 and return without executing the handler.
    """
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
    return current, claimed == 1


def _perform_transcription(job: ProcessingJob) -> dict:
    from models.interview import Interview, Transcription
    from services.transcription import get_default_transcription_model, run_transcription

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

    tr = Transcription(
        media_file_id=media.id,
        whisper_model=get_default_transcription_model(),
        language="ja",
        status="pending",
    )
    db.session.add(tr)
    db.session.commit()
    update_progress(job, "transcribing", transcription_id=tr.id)
    result = run_transcription(tr.id)
    return {"transcription_id": tr.id, **result}


def _perform_mapping(job: ProcessingJob) -> dict:
    from models.interview import Interview
    from services.mapper import run_mapping

    interview = db.session.get(Interview, job.interview_id)
    if not interview or interview.project_id != job.project_id:
        raise ValueError("interview not found in job project")
    update_progress(job, "mapping")
    count = run_mapping(interview.id)
    return {"mapped_count": count}


def _perform_analysis(job: ProcessingJob) -> dict:
    from models.interview import Interview
    from services.analyzer import analyze_interview_summary

    interview = db.session.get(Interview, job.interview_id)
    if not interview or interview.project_id != job.project_id:
        raise ValueError("interview not found in job project")
    update_progress(job, "analyzing")
    analysis = analyze_interview_summary(interview.id)
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
    """Atomically claim and execute one durable job in the current app context."""
    job, claimed = _claim_pending_job(job_id, worker_pid=worker_pid)
    if not claimed:
        return job

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
        job.status = "succeeded"
        job.result_json = _json_dump(result or {})
        job.progress_json = _json_dump({"stage": "completed"})
        job.finished_at = _utcnow()
        job.worker_pid = None
        db.session.add(job)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        job = db.session.get(ProcessingJob, job_id)
        job.status = "failed"
        partial_result = getattr(exc, "job_result", None)
        if partial_result is not None:
            job.result_json = _json_dump(partial_result)
            job.progress_json = _json_dump({
                "stage": "failed_partial",
                "failed_interview_count": partial_result.get("failed_interview_count", 0),
                "interview_count": partial_result.get("interview_count", 0),
            })
        else:
            job.progress_json = _json_dump({"stage": "failed"})
        job.error_message = _safe_error(exc)
        job.finished_at = _utcnow()
        job.worker_pid = None
        db.session.add(job)
        db.session.commit()
    return job
