from __future__ import annotations

from models.quote_candidate import QuoteCandidate


def get_approved_quote_candidates_for_interview(session, interview_id: int) -> list[dict]:
    """
    Return formal quote rows for output usage.
    Only QuoteCandidate.status='approved' is returned.
    """
    rows = (
        session.query(QuoteCandidate)
        .filter_by(interview_id=interview_id, status="approved")
        .order_by(QuoteCandidate.id.asc())
        .all()
    )
    return [
        {
            "quote_id": row.quote_id,
            "segment_id": row.segment_id,
            "participant_id": row.participant_id,
            "question_id": row.question_id,
            "start_sec": row.start_sec,
            "end_sec": row.end_sec,
            "quote_text": row.quote_text,
            "source": row.source,
            "status": row.status,
        }
        for row in rows
    ]

