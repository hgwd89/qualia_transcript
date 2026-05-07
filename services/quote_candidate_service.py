from __future__ import annotations

import uuid
from datetime import datetime, timezone

from models.quote_candidate import QuoteCandidate
from models.segment import Segment
from models.segment_flag import SegmentFlag

ALLOWED_QUOTE_CANDIDATE_SOURCES = {"human", "ai", "flag", "import"}
ALLOWED_QUOTE_CANDIDATE_STATUSES = {"candidate", "approved", "rejected"}
ACTIVE_DUPLICATE_STATUSES = {"candidate", "approved"}


class QuoteCandidateValidationError(ValueError):
    """Raised when quote candidate input is invalid."""


class QuoteCandidateNotFoundError(LookupError):
    """Raised when related interview/segment/quote candidate cannot be resolved."""


def _now():
    return datetime.now(timezone.utc)


def _base_text_for_segment(segment: Segment) -> str:
    reviewed_text = getattr(segment, "reviewed_text", None)
    if isinstance(reviewed_text, str) and reviewed_text:
        return reviewed_text
    return segment.text or ""


def _validate_quote_text(
    *,
    base_text: str,
    quote_text: str,
    char_start: int | None,
    char_end: int | None,
) -> None:
    if not quote_text or not quote_text.strip():
        raise QuoteCandidateValidationError("quote_text is required")

    if (char_start is None) != (char_end is None):
        raise QuoteCandidateValidationError("char_start and char_end must be provided together")

    if char_start is not None and char_end is not None:
        if char_start < 0 or char_end < 0 or char_start >= char_end:
            raise QuoteCandidateValidationError("invalid char_start/char_end range")
        if char_end > len(base_text):
            raise QuoteCandidateValidationError("char_end is out of range")
        extracted = base_text[char_start:char_end]
        if extracted != quote_text:
            raise QuoteCandidateValidationError("quote_text does not match base_text[char_start:char_end]")
        return

    if quote_text not in base_text:
        raise QuoteCandidateValidationError("quote_text must be included in segment base text")


def _new_quote_id(interview_id: int, segment_id: int) -> str:
    return f"QT-{interview_id}-{segment_id}-{uuid.uuid4().hex[:10].upper()}"


def _find_duplicate(
    session,
    *,
    interview_id: int,
    segment_id: int,
    quote_text: str,
    source: str,
):
    query = (
        session.query(QuoteCandidate)
        .filter(QuoteCandidate.interview_id == interview_id)
        .filter(QuoteCandidate.segment_id == segment_id)
        .filter(QuoteCandidate.quote_text == quote_text)
        .filter(QuoteCandidate.status.in_(tuple(ACTIVE_DUPLICATE_STATUSES)))
    )
    if source == "flag":
        query = query.filter(QuoteCandidate.source == "flag")
    return query.order_by(QuoteCandidate.id.desc()).first()


def create_quote_candidate(
    session,
    *,
    interview_id: int,
    segment_id: int,
    quote_text: str,
    source: str,
    char_start: int | None = None,
    char_end: int | None = None,
    note: str | None = None,
    participant_id: int | None = None,
    question_id: int | None = None,
) -> tuple[QuoteCandidate, bool]:
    if source not in ALLOWED_QUOTE_CANDIDATE_SOURCES:
        raise QuoteCandidateValidationError("invalid source")

    segment = session.get(Segment, segment_id)
    if not segment:
        raise QuoteCandidateNotFoundError("segment not found")
    if int(segment.interview_id) != int(interview_id):
        raise QuoteCandidateNotFoundError("segment does not belong to interview")

    base_text = _base_text_for_segment(segment)
    _validate_quote_text(
        base_text=base_text,
        quote_text=quote_text,
        char_start=char_start,
        char_end=char_end,
    )

    duplicate = _find_duplicate(
        session,
        interview_id=interview_id,
        segment_id=segment_id,
        quote_text=quote_text,
        source=source,
    )
    if duplicate:
        return duplicate, False

    quote = QuoteCandidate(
        quote_id=_new_quote_id(interview_id=interview_id, segment_id=segment_id),
        project_id=segment.interview.project_id if segment.interview else None,
        interview_id=interview_id,
        segment_id=segment.id,
        participant_id=participant_id if participant_id is not None else segment.participant_id,
        question_id=question_id,
        start_sec=segment.start_sec,
        end_sec=segment.end_sec,
        char_start=char_start,
        char_end=char_end,
        quote_text=quote_text,
        status="candidate",
        source=source,
        note=note,
    )
    session.add(quote)
    return quote, True


def create_quote_candidates_from_flags(session, *, interview_id: int) -> dict[str, int]:
    flags = (
        session.query(SegmentFlag)
        .join(Segment, Segment.id == SegmentFlag.segment_id)
        .filter(Segment.interview_id == interview_id)
        .filter(SegmentFlag.flag_type == "quote")
        .order_by(SegmentFlag.id.asc())
        .all()
    )

    created = 0
    existing = 0
    skipped = 0

    for flag in flags:
        segment = flag.segment
        if not segment or not segment.text:
            skipped += 1
            continue
        quote, was_created = create_quote_candidate(
            session,
            interview_id=interview_id,
            segment_id=segment.id,
            quote_text=segment.text,
            source="flag",
            note=flag.note,
        )
        if was_created:
            created += 1
        elif quote:
            existing += 1

    return {
        "created": int(created),
        "existing": int(existing),
        "skipped": int(skipped),
    }


def update_quote_candidate_status(
    session,
    *,
    interview_id: int,
    quote_id: str,
    status: str,
) -> QuoteCandidate:
    status = (status or "").strip().lower()
    if status not in {"approved", "rejected"}:
        raise QuoteCandidateValidationError("invalid status")

    quote = (
        session.query(QuoteCandidate)
        .filter_by(interview_id=interview_id, quote_id=quote_id)
        .first()
    )
    if not quote:
        raise QuoteCandidateNotFoundError("quote candidate not found")

    quote.status = status
    quote.updated_at = _now()
    return quote


def list_quote_candidates_for_interview(session, *, interview_id: int, status: str | None = None) -> list[QuoteCandidate]:
    query = (
        session.query(QuoteCandidate)
        .filter_by(interview_id=interview_id)
        .order_by(QuoteCandidate.id.desc())
    )
    if status:
        query = query.filter_by(status=status)
    return query.all()
