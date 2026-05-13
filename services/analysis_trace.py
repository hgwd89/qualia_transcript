from __future__ import annotations

from math import inf
from typing import Any

from models.quote_candidate import QuoteCandidate
from models.segment import Segment, UtteranceMapping
from models.segment_flag import SegmentFlag
from models.speaker_assignment import SpeakerAssignment


def build_per_question_trace(
    session,
    interview_id: int,
    question_id: int,
    include_needs_review: bool = False,
    max_segments: int | None = None,
) -> dict[str, Any]:
    """Build local evidence trace for per_question AIAnalysis.

    source_segment_quotes are resolved only from Segment.text. This helper never
    asks AI to generate quote text and does not write to the database.
    """
    mappings = (
        session.query(UtteranceMapping)
        .join(Segment, UtteranceMapping.segment_id == Segment.id)
        .filter(
            UtteranceMapping.question_id == question_id,
            Segment.interview_id == interview_id,
        )
        .all()
    )

    if not mappings:
        return {"source_segment_ids": [], "source_segment_quotes": [], "quote_ids": []}

    segment_ids = [mapping.segment_id for mapping in mappings]
    segments = {
        segment.id: segment
        for segment in session.query(Segment).filter(Segment.id.in_(segment_ids)).all()
    }

    flags_by_segment: dict[int, set[str]] = {segment_id: set() for segment_id in segment_ids}
    for flag in session.query(SegmentFlag).filter(SegmentFlag.segment_id.in_(segment_ids)).all():
        flags_by_segment.setdefault(flag.segment_id, set()).add(flag.flag_type)

    assignments = {
        assignment.speaker_label: assignment
        for assignment in session.query(SpeakerAssignment)
        .filter(SpeakerAssignment.interview_id == interview_id)
        .all()
    }

    approved_quote_candidates = (
        session.query(QuoteCandidate)
        .filter(
            QuoteCandidate.interview_id == interview_id,
            QuoteCandidate.segment_id.in_(segment_ids),
            QuoteCandidate.status == "approved",
        )
        .all()
    )
    approved_quotes_by_segment: dict[int, list[QuoteCandidate]] = {}
    for quote in approved_quote_candidates:
        approved_quotes_by_segment.setdefault(quote.segment_id, []).append(quote)

    trace_items = []
    for mapping in mappings:
        segment = segments.get(mapping.segment_id)
        if segment is None or segment.interview_id != interview_id:
            continue

        flags = flags_by_segment.get(segment.id, set())
        if "exclude" in flags:
            continue
        if "needs_review" in flags and not include_needs_review:
            continue

        assignment = assignments.get(segment.speaker_label)
        if assignment is None or assignment.speaker_role != "respondent":
            continue

        approved_quotes = approved_quotes_by_segment.get(segment.id, [])
        has_priority = "quote" in flags or bool(approved_quotes)
        trace_items.append(
            {
                "segment": segment,
                "assignment": assignment,
                "approved_quotes": approved_quotes,
                "has_priority": has_priority,
            }
        )

    trace_items.sort(
        key=lambda item: (
            0 if item["has_priority"] else 1,
            item["segment"].start_sec if item["segment"].start_sec is not None else inf,
            item["segment"].id,
        )
    )

    if max_segments is not None:
        trace_items = trace_items[:max_segments]

    source_segment_ids: list[int] = []
    source_segment_quotes: list[dict[str, Any]] = []
    quote_ids: list[str] = []

    for item in trace_items:
        segment = item["segment"]
        assignment = item["assignment"]
        source_segment_ids.append(segment.id)
        source_segment_quotes.append(
            {
                "segment_id": segment.id,
                "text": segment.text,
                "speaker_label": segment.speaker_label,
                "participant_id": assignment.participant_id or segment.participant_id,
                "start_sec": segment.start_sec,
                "end_sec": segment.end_sec,
            }
        )
        for quote in sorted(item["approved_quotes"], key=lambda q: q.quote_id):
            if quote.quote_id not in quote_ids:
                quote_ids.append(quote.quote_id)

    return {
        "source_segment_ids": source_segment_ids,
        "source_segment_quotes": source_segment_quotes,
        "quote_ids": quote_ids,
    }
