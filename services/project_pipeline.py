from __future__ import annotations

import re

from models import db
from models.interview import Interview, Transcription
from models.project import Project
from services.analyzer import analyze_interview_summary
from services.mapper import run_mapping
from services.transcription import (
    auto_assign_speaker_roles,
    get_default_transcription_model,
    run_transcription,
)


_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]+")
_TERMINAL_INTERVIEW_STATUSES = {"analyzed", "done"}


def _safe_error(exc: Exception) -> str:
    return _KEY_RE.sub("[REDACTED_KEY]", str(exc or ""))[:1000]


class ProjectPipelinePartialFailure(RuntimeError):
    """All interviews were inspected/attempted, but one or more were incomplete."""

    def __init__(self, result: dict):
        self.job_result = result
        failed = int(result.get("failed_interview_count") or 0)
        super().__init__(f"project pipeline completed with {failed} incomplete/failed interview(s)")


def _step_error(row: dict, step: str, exc: Exception, *, code: str | None = None) -> None:
    item = {
        "step": step,
        "error": _safe_error(exc),
        "error_type": type(exc).__name__,
    }
    if code:
        item["code"] = code
    row["steps"].append(item)


def _status(interview_id: int) -> str | None:
    interview = db.session.get(Interview, interview_id)
    return interview.status if interview else None


def run_project_pipeline(job, update_progress) -> dict:
    project = db.session.get(Project, job.project_id)
    if not project:
        raise ValueError("project not found")

    # Do not silently drop interviews already marked error. They remain part of
    # the project and must make an all-project operation visibly incomplete.
    interviews = list(project.interviews)
    first_flow_id = project.interview_flows[0].id if project.interview_flows else None
    results = []

    for index, source_interview in enumerate(interviews, start=1):
        interview_id = source_interview.id
        row = {"interview_id": interview_id, "steps": []}
        update_progress(
            job,
            "project_pipeline",
            current=index,
            total=len(interviews),
            interview_id=interview_id,
            interview_status=source_interview.status,
        )

        interview = db.session.get(Interview, interview_id)
        if not interview:
            _step_error(
                row,
                "load_interview",
                RuntimeError("interview disappeared during pipeline"),
                code="missing_interview",
            )
            results.append(row)
            continue

        if interview.status == "error":
            _step_error(
                row,
                "precheck",
                RuntimeError("interview is already in error status"),
                code="interview_error_status",
            )
            results.append(row)
            continue

        if not interview.flow_id and first_flow_id:
            try:
                interview.flow_id = first_flow_id
                db.session.commit()
                row["steps"].append({"step": "assign_flow", "result": first_flow_id})
            except Exception as exc:
                db.session.rollback()
                _step_error(row, "assign_flow", exc)
                results.append(row)
                continue

        if interview.status == "pending":
            if not interview.media_files:
                _step_error(
                    row,
                    "transcribe",
                    RuntimeError("audio file is not registered"),
                    code="missing_media",
                )
                results.append(row)
                continue

            media = interview.media_files[-1]
            existing = (
                Transcription.query
                .filter_by(media_file_id=media.id, status="done")
                .order_by(Transcription.id.desc())
                .first()
            )
            if existing:
                interview.status = "transcribed"
                db.session.commit()
                row["steps"].append({
                    "step": "transcribe",
                    "result": "already_done",
                    "transcription_id": existing.id,
                })
            else:
                try:
                    tr = Transcription(
                        media_file_id=media.id,
                        whisper_model=get_default_transcription_model(),
                        language="ja",
                        status="pending",
                    )
                    db.session.add(tr)
                    db.session.commit()
                    result = run_transcription(tr.id)
                    if _status(interview_id) != "transcribed":
                        raise RuntimeError("transcription did not advance interview status to transcribed")
                    row["steps"].append({
                        "step": "transcribe",
                        "result": result,
                        "transcription_id": tr.id,
                    })
                except Exception as exc:
                    db.session.rollback()
                    _step_error(row, "transcribe", exc, code="transcription_incomplete")
                    results.append(row)
                    continue

        interview = db.session.get(Interview, interview_id)
        if interview and interview.status == "transcribed":
            try:
                auto_assign_speaker_roles(interview.id)
                row["steps"].append({"step": "auto_roles", "result": "ok"})
            except Exception as exc:
                db.session.rollback()
                _step_error(row, "auto_roles", exc)
                results.append(row)
                continue

        interview = db.session.get(Interview, interview_id)
        if interview and interview.status == "transcribed":
            try:
                count = run_mapping(interview.id)
                if _status(interview_id) != "mapped":
                    raise RuntimeError(
                        "mapping did not advance interview status to mapped; verify flow, questions, and respondent segments"
                    )
                row["steps"].append({"step": "mapping", "result": count})
            except Exception as exc:
                db.session.rollback()
                _step_error(row, "mapping", exc, code="mapping_incomplete")
                results.append(row)
                continue

        interview = db.session.get(Interview, interview_id)
        if interview and interview.status == "mapped":
            try:
                analysis = analyze_interview_summary(interview.id)
                if _status(interview_id) != "analyzed":
                    raise RuntimeError("analysis did not advance interview status to analyzed")
                row["steps"].append({"step": "analyze", "result": analysis.id})
            except Exception as exc:
                db.session.rollback()
                _step_error(row, "analyze", exc, code="analysis_incomplete")
                results.append(row)
                continue

        final_status = _status(interview_id)
        if final_status not in _TERMINAL_INTERVIEW_STATUSES:
            _step_error(
                row,
                "finalize",
                RuntimeError(f"interview remained incomplete with status={final_status}"),
                code="pipeline_incomplete_status",
            )

        results.append(row)

    failed_rows = [row for row in results if any(step.get("error") for step in row["steps"])]
    missing_media_count = sum(
        1
        for row in results
        for step in row["steps"]
        if step.get("code") == "missing_media"
    )
    error_status_count = sum(
        1
        for row in results
        for step in row["steps"]
        if step.get("code") == "interview_error_status"
    )
    payload = {
        "interviews": results,
        "interview_count": len(interviews),
        "failed_interview_count": len(failed_rows),
        "completed_interview_count": len(interviews) - len(failed_rows),
        "missing_media_count": missing_media_count,
        "error_status_count": error_status_count,
    }

    if failed_rows:
        raise ProjectPipelinePartialFailure(payload)
    return payload
