from __future__ import annotations

import re

from models import db
from models.interview import Interview, Transcription
from models.project import Project
from services.analyzer import analyze_interview_summary
from services.mapper import run_mapping
from services.processing_jobs import JobLeaseLost, assert_job_lease, begin_job_result_write
from services.processing_result_guard import discard_incomplete_transcription_segments
from services.transcription import (
    auto_assign_speaker_roles,
    get_default_transcription_model,
)
from services.transcription_dispatch import run_transcription


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
                begin_job_result_write(job)
                interview.flow_id = first_flow_id
                db.session.commit()
                row["steps"].append({"step": "assign_flow", "result": first_flow_id})
            except JobLeaseLost:
                db.session.rollback()
                raise
            except Exception as exc:
                db.session.rollback()
                _step_error(row, "assign_flow", exc)
                results.append(row)
                continue

        if interview.status == "pending":
            assert_job_lease(job)
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
                begin_job_result_write(job)
                interview.status = "transcribed"
                db.session.commit()
                row["steps"].append({
                    "step": "transcribe",
                    "result": "already_done",
                    "transcription_id": existing.id,
                })
            else:
                try:
                    # Cleanup mutates transcription/segment rows and commits. Fence
                    # that write separately so an old pipeline attempt cannot delete
                    # partial rows belonging to a newer retry.
                    begin_job_result_write(job)
                    cleanup = discard_incomplete_transcription_segments(media.id)

                    # Creating the next canonical attempt is another commit boundary.
                    # Revalidate the immutable attempt token after cleanup completed.
                    begin_job_result_write(job)
                    tr = Transcription(
                        media_file_id=media.id,
                        whisper_model=get_default_transcription_model(),
                        language="ja",
                        status="pending",
                    )
                    db.session.add(tr)
                    db.session.commit()
                    result = run_transcription(
                        tr.id,
                        lease_check=lambda: assert_job_lease(job),
                        result_write_guard=lambda: begin_job_result_write(job),
                    )
                    if _status(interview_id) != "transcribed":
                        raise RuntimeError("transcription did not advance interview status to transcribed")
                    row["steps"].append({
                        "step": "transcribe",
                        "result": result,
                        "transcription_id": tr.id,
                        "discarded_partial_segment_count": cleanup["deleted_segment_count"],
                    })
                except JobLeaseLost:
                    db.session.rollback()
                    raise
                except Exception as exc:
                    db.session.rollback()
                    _step_error(row, "transcribe", exc, code="transcription_incomplete")
                    results.append(row)
                    continue

        interview = db.session.get(Interview, interview_id)
        if interview and interview.status == "transcribed":
            try:
                # auto_assign_speaker_roles() can commit internally. Acquire the
                # result-write reservation first; an extra commit also releases the
                # reservation when the helper returns early without changing rows.
                begin_job_result_write(job)
                auto_assign_speaker_roles(interview.id)
                db.session.commit()
                row["steps"].append({"step": "auto_roles", "result": "ok"})
            except JobLeaseLost:
                db.session.rollback()
                raise
            except Exception as exc:
                db.session.rollback()
                _step_error(row, "auto_roles", exc)
                results.append(row)
                continue

        interview = db.session.get(Interview, interview_id)
        if interview and interview.status == "transcribed":
            assert_job_lease(job)
            try:
                count = run_mapping(
                    interview.id,
                    result_write_guard=lambda: begin_job_result_write(job),
                )
                assert_job_lease(job)
                if _status(interview_id) != "mapped":
                    raise RuntimeError(
                        "mapping did not advance interview status to mapped; verify flow, questions, and respondent segments"
                    )
                row["steps"].append({"step": "mapping", "result": count})
            except JobLeaseLost:
                db.session.rollback()
                raise
            except Exception as exc:
                db.session.rollback()
                _step_error(row, "mapping", exc, code="mapping_incomplete")
                results.append(row)
                continue

        interview = db.session.get(Interview, interview_id)
        if interview and interview.status == "mapped":
            assert_job_lease(job)
            try:
                analysis = analyze_interview_summary(
                    interview.id,
                    result_write_guard=lambda: begin_job_result_write(job),
                )
                assert_job_lease(job)
                if _status(interview_id) != "analyzed":
                    raise RuntimeError("analysis did not advance interview status to analyzed")
                row["steps"].append({"step": "analyze", "result": analysis.id})
            except JobLeaseLost:
                db.session.rollback()
                raise
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
