from __future__ import annotations

from datetime import datetime, timezone

from models import db
from models.analysis import AIAnalysis
from models.interview import Transcription
from models.segment import Segment
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
        # ORM delete preserves relationship cascades (mappings/flags) instead of
        # leaving child rows behind through a bulk delete.
        db.session.delete(segment)
    return len(segments)


def discard_transcription_segments(transcription_id: int) -> int:
    """Delete partial segments for one transcription before an in-place fallback."""
    deleted = _delete_segments_for_transcription_ids([int(transcription_id)])
    db.session.commit()
    return deleted


def invalidate_transcription_attempt(transcription_id: int) -> dict:
    """Invalidate result rows written by a worker that lost its durable job lease.

    The transcription row remains as audit history, while segments from that stale
    attempt are removed. Interview status is only rewound when no other completed
    transcription exists for the same media, avoiding clobbering a newer attempt.
    """
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
    """Remove non-canonical segments left by incomplete transcription attempts.

    Long-audio transcription commits completed chunks incrementally. If a later
    chunk fails or the worker dies, those committed segments must not survive
    into a fresh retry or they will be duplicated when the audio is processed
    again. Completed (`done`) transcriptions are never touched.
    """
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


def find_completed_analysis_for_job(job: ProcessingJob) -> AIAnalysis | None:
    """Find a participant analysis already committed after this job was created.

    This closes the crash window where `analyze_interview_summary()` commits the
    AIAnalysis and interview status, but the worker dies before the durable job
    can persist its terminal success. An earlier analysis created before this job
    remains eligible for intentional regeneration and is not reused.
    """
    if job.job_type != "analyze" or job.interview_id is None or job.created_at is None:
        return None

    return (
        AIAnalysis.query
        .filter_by(
            interview_id=int(job.interview_id),
            analysis_type="per_participant",
        )
        .filter(AIAnalysis.created_at >= job.created_at)
        .order_by(AIAnalysis.id.desc())
        .first()
    )
