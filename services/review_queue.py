from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from models.analysis import AIAnalysis
from models.interview import Interview
from models.quote_candidate import QuoteCandidate
from models.review_item import ReviewItem
from models.segment import Segment, UtteranceMapping
from models.segment_flag import SegmentFlag
from models.speaker_assignment import SpeakerAssignment


def _now():
    return datetime.now(timezone.utc)


def _active_key(project_id: int | None, interview_id: int | None, item_type: str, target_type: str, target_id: int):
    return (project_id, interview_id, item_type, target_type, target_id)


def _sync_review_item_with_state(
    session,
    project_id: int | None,
    interview_id: int | None,
    item_type: str,
    target_type: str,
    target_id: int,
    severity: str,
    reason: str | None,
) -> tuple[ReviewItem | None, bool, bool]:
    """
    Returns:
      (item_or_none, created, skipped_by_ignored)
    """
    open_item = (
        session.query(ReviewItem)
        .filter_by(
            project_id=project_id,
            interview_id=interview_id,
            item_type=item_type,
            target_type=target_type,
            target_id=target_id,
            status="open",
        )
        .order_by(ReviewItem.id.desc())
        .first()
    )
    if open_item:
        # Keep the latest reason/severity fresh for active open items.
        if reason:
            open_item.reason = reason
        if severity:
            open_item.severity = severity
        open_item.updated_at = _now()
        return open_item, False, False

    ignored_item = (
        session.query(ReviewItem)
        .filter_by(
            project_id=project_id,
            interview_id=interview_id,
            item_type=item_type,
            target_type=target_type,
            target_id=target_id,
            status="ignored",
        )
        .order_by(ReviewItem.updated_at.desc(), ReviewItem.id.desc())
        .first()
    )
    if ignored_item:
        # Do not auto-reopen ignored items.
        return ignored_item, False, True

    created = ReviewItem(
        project_id=project_id,
        interview_id=interview_id,
        item_type=item_type,
        target_type=target_type,
        target_id=target_id,
        severity=severity or "medium",
        status="open",
        reason=reason,
    )
    session.add(created)
    return created, True, False


def sync_review_item(
    session,
    project_id: int | None,
    interview_id: int | None,
    item_type: str,
    target_type: str,
    target_id: int,
    severity: str = "medium",
    reason: str | None = None,
) -> ReviewItem | None:
    item, _, _ = _sync_review_item_with_state(
        session=session,
        project_id=project_id,
        interview_id=interview_id,
        item_type=item_type,
        target_type=target_type,
        target_id=target_id,
        severity=severity,
        reason=reason,
    )
    return item


def resolve_missing_open_items(session, interview_id: int, active_keys: set[tuple[Any, ...]]) -> int:
    open_rows = (
        session.query(ReviewItem)
        .filter_by(interview_id=interview_id, status="open")
        .all()
    )
    resolved = 0
    for row in open_rows:
        key = _active_key(row.project_id, row.interview_id, row.item_type, row.target_type, row.target_id)
        if key in active_keys:
            continue
        row.status = "resolved"
        row.updated_at = _now()
        resolved += 1
    return resolved


def _collect_interview_candidates(session, interview_id: int):
    """Collect active review targets for one interview."""
    interview = session.get(Interview, interview_id)
    if not interview:
        raise ValueError(f"interview_id={interview_id} not found")
    project_id = interview.project_id

    items: list[dict[str, Any]] = []

    # 1) unclassified_mapping
    unclassified_rows = (
        session.query(UtteranceMapping)
        .join(Segment, Segment.id == UtteranceMapping.segment_id)
        .filter(Segment.interview_id == interview_id)
        .filter(UtteranceMapping.is_unclassified.is_(True))
        .all()
    )
    for row in unclassified_rows:
        items.append({
            "project_id": project_id,
            "interview_id": interview_id,
            "item_type": "unclassified_mapping",
            "target_type": "utterance_mapping",
            "target_id": int(row.id),
            "severity": "high",
            "reason": "UtteranceMapping.is_unclassified=true",
        })

    # 2) low_confidence_mapping (exclude unclassified to avoid duplication)
    low_conf_rows = (
        session.query(UtteranceMapping)
        .join(Segment, Segment.id == UtteranceMapping.segment_id)
        .filter(Segment.interview_id == interview_id)
        .filter(UtteranceMapping.is_unclassified.is_(False))
        .filter(UtteranceMapping.confidence_level.in_(("medium", "low")))
        .all()
    )
    for row in low_conf_rows:
        items.append({
            "project_id": project_id,
            "interview_id": interview_id,
            "item_type": "low_confidence_mapping",
            "target_type": "utterance_mapping",
            "target_id": int(row.id),
            "severity": "medium",
            "reason": f"confidence_level={row.confidence_level}",
        })

    # 3) speaker_unassigned
    speaker_rows = (
        session.query(SpeakerAssignment)
        .filter_by(interview_id=interview_id, speaker_role="respondent")
        .filter(SpeakerAssignment.participant_id.is_(None))
        .all()
    )
    for row in speaker_rows:
        items.append({
            "project_id": project_id,
            "interview_id": interview_id,
            "item_type": "speaker_unassigned",
            "target_type": "speaker_assignment",
            "target_id": int(row.id),
            "severity": "high",
            "reason": "respondent speaker has no participant_id",
        })

    # 4) quote_candidate
    quote_rows = (
        session.query(QuoteCandidate)
        .filter_by(interview_id=interview_id, status="candidate")
        .all()
    )
    for row in quote_rows:
        items.append({
            "project_id": project_id,
            "interview_id": interview_id,
            "item_type": "quote_candidate",
            "target_type": "quote_candidate",
            "target_id": int(row.id),
            "severity": "medium",
            "reason": "QuoteCandidate.status=candidate",
        })

    # 5) needs_review_segment
    needs_rows = (
        session.query(SegmentFlag)
        .join(Segment, Segment.id == SegmentFlag.segment_id)
        .filter(Segment.interview_id == interview_id)
        .filter(SegmentFlag.flag_type == "needs_review")
        .all()
    )
    for row in needs_rows:
        items.append({
            "project_id": project_id,
            "interview_id": interview_id,
            "item_type": "needs_review_segment",
            "target_type": "segment",
            "target_id": int(row.segment_id),
            "severity": "high",
            "reason": "SegmentFlag.flag_type=needs_review",
        })

    # 6) ai_analysis_draft
    draft_rows = (
        session.query(AIAnalysis)
        .filter_by(interview_id=interview_id, status="draft")
        .all()
    )
    for row in draft_rows:
        items.append({
            "project_id": project_id,
            "interview_id": interview_id,
            "item_type": "ai_analysis_draft",
            "target_type": "ai_analysis",
            "target_id": int(row.id),
            "severity": "medium",
            "reason": "AIAnalysis.status=draft",
        })

    return items


def rebuild_review_items_for_interview(session, interview_id: int, resolve_missing: bool = True) -> dict[str, int]:
    items = _collect_interview_candidates(session, interview_id)
    created = 0
    existing = 0
    resolved = 0

    active_keys: set[tuple[Any, ...]] = set()
    for item in items:
        key = _active_key(
            item["project_id"],
            item["interview_id"],
            item["item_type"],
            item["target_type"],
            item["target_id"],
        )
        active_keys.add(key)
        _, was_created, _ = _sync_review_item_with_state(
            session=session,
            project_id=item["project_id"],
            interview_id=item["interview_id"],
            item_type=item["item_type"],
            target_type=item["target_type"],
            target_id=item["target_id"],
            severity=item["severity"],
            reason=item["reason"],
        )
        if was_created:
            created += 1
        else:
            existing += 1

    if resolve_missing:
        resolved = resolve_missing_open_items(session, interview_id=interview_id, active_keys=active_keys)

    session.commit()
    open_total = (
        session.query(ReviewItem)
        .filter_by(interview_id=interview_id, status="open")
        .count()
    )
    return {
        "created": int(created),
        "existing": int(existing),
        "resolved": int(resolved),
        "open_total": int(open_total),
    }


def rebuild_review_items_for_project(session, project_id: int, resolve_missing: bool = True) -> dict[str, int]:
    interview_ids = [
        x[0]
        for x in session.query(Interview.id).filter_by(project_id=project_id).all()
    ]
    created = 0
    existing = 0
    resolved = 0
    for interview_id in interview_ids:
        s = rebuild_review_items_for_interview(session, interview_id, resolve_missing=resolve_missing)
        created += int(s["created"])
        existing += int(s["existing"])
        resolved += int(s["resolved"])

    open_total = (
        session.query(ReviewItem)
        .filter_by(project_id=project_id, status="open")
        .count()
    )
    return {
        "created": int(created),
        "existing": int(existing),
        "resolved": int(resolved),
        "open_total": int(open_total),
    }
