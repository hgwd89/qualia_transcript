from __future__ import annotations

from dataclasses import dataclass
import json

from sqlalchemy import text

from models import db
from models.interview import Interview
from models.interview_flow import InterviewFlowQuestion
from models.processing_job import ProcessingJob
from models.project import Project
from services.job_recovery import recover_stale_jobs
from services.processing_jobs import ACTIVE_STATUSES, JOB_TYPES, _json_dump, _utcnow
from services.project_flow_scope import ProjectFlowScopeError, resolve_integrated_analysis_scope


PROJECT_EXCLUSIVE_JOB_TYPES = {"project_pipeline", "analyze_cross", "analyze_integrated"}
_JOB_SCOPE = {
    "transcribe": (True, False),
    "map": (True, False),
    "analyze": (True, False),
    "analyze_semantic": (True, False),
    "analyze_question": (True, True),
    "analyze_cross": (False, True),
    "analyze_integrated": (False, False),
    "project_pipeline": (False, False),
}


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


def _validate_job_scope(
    project_id: int,
    job_type: str,
    interview_id: int | None,
    question_id: int | None,
) -> None:
    """Validate durable-job ownership and required/forbidden scope fields.

    This runs after the admission write reservation is acquired so project
    deletion, scope-changing writes, and job admission observe one serialized
    database snapshot.
    """
    project_id = int(project_id)
    project = db.session.get(Project, project_id) if project_id > 0 else None
    if project is None:
        raise ValueError("project not found for processing job")

    try:
        interview_required, question_required = _JOB_SCOPE[job_type]
    except KeyError as exc:
        raise ValueError(f"unsupported job_type: {job_type}") from exc

    if interview_required and interview_id is None:
        raise ValueError(f"{job_type} processing job requires interview_id")
    if not interview_required and interview_id is not None:
        raise ValueError(f"{job_type} processing job forbids interview_id")
    if question_required and question_id is None:
        raise ValueError(f"{job_type} processing job requires question_id")
    if not question_required and question_id is not None:
        raise ValueError(f"{job_type} processing job forbids question_id")

    interview = None
    if interview_id is not None:
        interview = db.session.get(Interview, int(interview_id))
        if not interview or int(interview.project_id) != project_id:
            raise ValueError("interview not found in processing job project")

    question = None
    if question_id is not None:
        question = db.session.get(InterviewFlowQuestion, int(question_id))
        if (
            not question
            or not question.section
            or not question.section.flow
            or int(question.section.flow.project_id) != project_id
        ):
            raise ValueError("question not found in processing job project")

    if interview is not None and question is not None:
        if not interview.flow_id or int(question.section.flow_id) != int(interview.flow_id):
            raise ValueError("question not found in processing job interview flow")

    # Integrated analysis has a stronger research-provenance contract than the
    # generic durable-job shape. Revalidate it *inside* the serialized admission
    # transaction so a concurrent flow/interview edit cannot slip between route
    # preflight and durable job creation.
    if job_type == "analyze_integrated":
        try:
            resolve_integrated_analysis_scope(project)
        except ProjectFlowScopeError as exc:
            raise ValueError(f"{exc.code}: {exc}") from exc


def _normalize_request_payload(job_type: str, request_payload: dict | None) -> dict | None:
    if job_type != "analyze_semantic":
        if request_payload not in (None, {}):
            raise ValueError(f"{job_type} processing job does not accept request payload")
        return None

    raw = request_payload or {}
    if not isinstance(raw, dict):
        raise ValueError("analyze_semantic request payload must be an object")
    unknown = set(raw) - {"max_segments", "no_ai"}
    if unknown:
        raise ValueError(f"unsupported analyze_semantic request field(s): {', '.join(sorted(unknown))}")

    max_segments = raw.get("max_segments")
    if max_segments is not None:
        try:
            max_segments = int(max_segments)
        except (TypeError, ValueError) as exc:
            raise ValueError("analyze_semantic max_segments must be a positive integer or null") from exc
        if max_segments <= 0:
            raise ValueError("analyze_semantic max_segments must be a positive integer or null")

    no_ai = raw.get("no_ai", False)
    if not isinstance(no_ai, bool):
        raise ValueError("analyze_semantic no_ai must be boolean")
    return {"max_segments": max_segments, "no_ai": no_ai}


def _canonical_request_json(payload: dict | None) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _job_request_json(job: ProcessingJob) -> str | None:
    if job.request_json:
        try:
            payload = json.loads(job.request_json)
        except (TypeError, json.JSONDecodeError):
            return str(job.request_json)
        return _canonical_request_json(payload)
    if job.job_type == "analyze_semantic":
        return _canonical_request_json({"max_segments": None, "no_ai": False})
    return None


def _same_request(job: ProcessingJob, request_json: str | None) -> bool:
    return _job_request_json(job) == request_json


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
    request_payload: dict | None = None,
) -> JobAdmission:
    """Atomically validate, reuse, reject, or create one processing job.

    SQLite `BEGIN IMMEDIATE` serializes all callers of this admission path before
    they validate project ownership or inspect the active-job set. The scope
    decision and optional INSERT therefore happen in one database transaction.
    """
    if job_type not in JOB_TYPES:
        raise ValueError(f"unsupported job_type: {job_type}")

    project_id = int(project_id)
    interview_id = int(interview_id) if interview_id is not None else None
    question_id = int(question_id) if question_id is not None else None
    normalized_request = _normalize_request_payload(job_type, request_payload)
    request_json = _canonical_request_json(normalized_request)

    recover_stale_jobs(project_id=project_id)
    try:
        _begin_immediate()
        try:
            _validate_job_scope(project_id, job_type, interview_id, question_id)
        except ValueError as exc:
            db.session.rollback()
            return JobAdmission(error=str(exc))

        active = _active_jobs(project_id)

        for job in active:
            if _same_scope(job, job_type, interview_id, question_id):
                db.session.commit()
                if _same_request(job, request_json):
                    return JobAdmission(job_id=job.id, created=False)
                return JobAdmission(job_id=job.id, conflict_job_id=job.id)

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
            request_json=request_json,
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
    """Atomically validate scope/conflicts and requeue exactly one failed job."""
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

        try:
            _validate_job_scope(
                int(target.project_id),
                target.job_type,
                int(target.interview_id) if target.interview_id is not None else None,
                int(target.question_id) if target.question_id is not None else None,
            )
        except ValueError as exc:
            db.session.rollback()
            return JobAdmission(job_id=target.id, error=f"invalid processing job scope: {exc}")

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
        # `created_at` is historical identity and may be hours/days old. While a
        # retry is pending, `started_at` temporarily anchors the *current* launch
        # grace. `_claim_pending_job()` overwrites it with the real worker start.
        target.started_at = _utcnow()
        target.finished_at = None
        db.session.commit()
        return JobAdmission(job_id=target.id, created=True)
    except Exception:
        db.session.rollback()
        raise