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


def launch_job_worker(job_id: int) -> int:
    """Launch a detached worker process and persist its PID."""
    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError("processing job not found")
    if job.status not in ACTIVE_STATUSES:
        raise ValueError(f"job cannot be launched from status={job.status}")

    root = Path(config.BASE_DIR).resolve()
    script = root / "scripts" / "run_processing_job.py"
    if not script.is_file():
        raise FileNotFoundError(f"worker script not found: {script}")

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

    with log_path.open("ab", buffering=0) as log_handle:
        process = subprocess.Popen(
            [sys.executable, str(script), "--job-id", str(job.id)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **kwargs,
        )

    job.worker_pid = process.pid
    job.progress_json = _json_dump({"stage": "worker_started", "pid": process.pid})
    db.session.add(job)
    db.session.commit()
    return process.pid


def retry_failed_job(job: ProcessingJob) -> ProcessingJob:
    if job.status != "failed":
        raise ValueError("only failed jobs can be retried")
    job.status = "pending"
    job.progress_json = _json_dump({"stage": "retry_queued"})
    job.result_json = None
    job.error_message = None
    job.worker_pid = None
    job.started_at = None
    job.finished_at = None
    db.session.add(job)
    db.session.commit()
    return job


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


def execute_job(job_id: int, handlers: dict[str, object] | None = None) -> ProcessingJob:
    """Claim and execute one durable job in the current Flask app context."""
    job = db.session.get(ProcessingJob, job_id)
    if not job:
        raise ValueError("processing job not found")
    if job.status != "pending":
        return job

    job.status = "running"
    job.started_at = _utcnow()
    job.finished_at = None
    job.attempt_count = int(job.attempt_count or 0) + 1
    job.error_message = None
    db.session.add(job)
    db.session.commit()

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
