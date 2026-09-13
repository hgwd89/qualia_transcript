from __future__ import annotations

from datetime import datetime, timezone

from models import db
from models.analysis import AIAnalysis
from models.interview import Interview, Transcription
from models.segment import Segment, UtteranceMapping
from models.processing_job import ProcessingJob


_SUPERSEDED_MESSAGE = "superseded by a later transcription attempt"
_STALE_ATTEMPT_MESSAGE = "stale processing attempt lost durable job lease"


def _delete_segments_for_transcription_ids(transcription_ids: list[int]) -> int:
    if not transcription_ids:
        return 0
    segments = (
        Segment.query
        .filter(Segment.transcription_id.in_([int(value) for value in transcription_ids]))
        .all()
    )
    for segment in segments:
        db.session.delete(segment)
    return len(segments)


def discard_transcription_segments(transcription_id: int) -> int:
    """Delete partial segments for one transcription before an in-place fallback."""
    deleted = _delete_segments_for_transcription_ids([int(transcription_id)])
    db.session.commit()
    return deleted


def invalidate_transcription_attempt(transcription_id: int) -> dict:
    """Invalidate result rows written by a worker that lost its durable job lease."""
    tr = db.session.get(Transcription, int(transcription_id))
    if not tr:
        return {"transcription_id": int(transcription_id), "deleted_segment_count": 0}

    deleted = _delete_segments_for_transcription_ids([int(tr.id)])
    tr.status = "error"
    tr.error_message = _STALE_ATTEMPT_MESSAGE
    tr.completed_at = datetime.now(timezone.utc)

    other_done = (
        Transcription.query
        .filter_by(media_file_id=int(tr.media_file_id), status="done")
        .filter(Transcription.id != int(tr.id))
        .first()
    )
    interview = tr.media_file.interview if tr.media_file else None
    if interview and not other_done and interview.status == "transcribed":
        interview.status = "pending"

    db.session.commit()
    return {"transcription_id": int(tr.id), "deleted_segment_count": deleted}


def discard_incomplete_transcription_segments(media_file_id: int) -> dict:
    """Remove non-canonical segments left by incomplete transcription attempts."""
    incomplete = (
        Transcription.query
        .filter_by(media_file_id=int(media_file_id))
        .filter(Transcription.status != "done")
        .order_by(Transcription.id.asc())
        .all()
    )
    transcription_ids = [int(tr.id) for tr in incomplete]
    if not transcription_ids:
        return {"transcription_ids": [], "deleted_segment_count": 0}

    deleted_segment_count = _delete_segments_for_transcription_ids(transcription_ids)

    now = datetime.now(timezone.utc)
    for tr in incomplete:
        if tr.status in {"pending", "running"}:
            tr.status = "error"
            tr.completed_at = now
        if not tr.error_message:
            tr.error_message = _SUPERSEDED_MESSAGE

    db.session.commit()
    return {
        "transcription_ids": transcription_ids,
        "deleted_segment_count": deleted_segment_count,
    }


def _attempt_result_anchor(job: ProcessingJob):
    """Return the start of the currently claimed durable attempt.

    `created_at` identifies the durable job row and survives retries. Result
    reconciliation must instead stay inside the currently running attempt or a
    retry can adopt an artifact committed by an older attempt after inputs have
    changed. `started_at` is rewritten atomically by every pending->running claim.
    The fallback is retained only for legacy rows that predate started_at.
    """
    return job.started_at or job.created_at


def find_completed_mapping_count_for_job(job: ProcessingJob) -> int | None:
    """Return a mapping result committed by the current durable attempt.

    Mapping replacement and interview status are committed atomically. Restrict
    recovery to rows created after the current attempt started so a retry cannot
    silently adopt mappings from an older attempt.
    """
    anchor = _attempt_result_anchor(job)
    if job.job_type != "map" or job.interview_id is None or anchor is None:
        return None

    interview = db.session.get(Interview, int(job.interview_id))
    if not interview or interview.status not in {"mapped", "analyzed", "done"}:
        return None

    count = (
        UtteranceMapping.query
        .join(Segment, UtteranceMapping.segment_id == Segment.id)
        .filter(
            Segment.interview_id == int(job.interview_id),
            Segment.speaker_role == "respondent",
            UtteranceMapping.created_at >= anchor,
        )
        .count()
    )
    return int(count) if count > 0 else None


def find_completed_analysis_for_scope(
    job: ProcessingJob,
    analysis_type: str,
) -> AIAnalysis | None:
    """Find a scoped analysis committed by the current durable attempt."""
    anchor = _attempt_result_anchor(job)
    if anchor is None:
        return None

    query = (
        AIAnalysis.query
        .filter_by(project_id=int(job.project_id), analysis_type=analysis_type)
        .filter(AIAnalysis.created_at >= anchor)
    )
    if job.interview_id is None:
        query = query.filter(AIAnalysis.interview_id.is_(None))
    else:
        query = query.filter(AIAnalysis.interview_id == int(job.interview_id))

    if job.question_id is None:
        query = query.filter(AIAnalysis.question_id.is_(None))
    else:
        query = query.filter(AIAnalysis.question_id == int(job.question_id))

    return query.order_by(AIAnalysis.id.desc()).first()


def find_completed_analysis_for_job(job: ProcessingJob) -> AIAnalysis | None:
    """Find a participant analysis committed by the current durable attempt."""
    if job.job_type != "analyze" or job.interview_id is None:
        return None
    return find_completed_analysis_for_scope(job, "per_participant")


def find_completed_result_for_active_job(job: ProcessingJob) -> dict | None:
    """Return the canonical result already committed by this running attempt.

    Stale-worker recovery calls this before converting an active job to failed.
    If the domain result committed after `started_at`, the worker died only in the
    narrow window before `_finish_job_success()`. Recover that exact attempt as
    succeeded instead of forcing a retry that could duplicate paid/provider work.
    """
    if job.status != "running":
        return None

    if job.job_type == "map":
        count = find_completed_mapping_count_for_job(job)
        if count is not None:
            return {"mapped_count": count, "recovered_committed_result": True}
        return None

    analysis = None
    result: dict | None = None
    if job.job_type == "analyze":
        analysis = find_completed_analysis_for_job(job)
        if analysis is not None:
            result = {"analysis_id": int(analysis.id)}
    elif job.job_type == "analyze_question":
        analysis = find_completed_analysis_for_scope(job, "per_question")
        if analysis is not None:
            result = {
                "analysis_id": int(analysis.id),
                "question_id": int(job.question_id),
            }
    elif job.job_type == "analyze_cross":
        analysis = find_completed_analysis_for_scope(job, "cross_participant")
        if analysis is not None:
            result = {
                "analysis_id": int(analysis.id),
                "question_id": int(job.question_id),
            }
    elif job.job_type == "analyze_integrated":
        analysis = find_completed_analysis_for_scope(job, "integrated")
        if analysis is not None:
            result = {"analysis_id": int(analysis.id)}

    if result is not None:
        result["recovered_committed_result"] = True
    return result
