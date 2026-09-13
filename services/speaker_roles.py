from __future__ import annotations

from collections.abc import Iterable

from models.speaker_assignment import SpeakerAssignment


def load_assignment_maps(interview_ids: Iterable[int]) -> dict[int, dict[str, SpeakerAssignment]]:
    """Load human speaker assignments keyed by interview ID and speaker label."""
    ids = sorted({int(value) for value in interview_ids})
    maps: dict[int, dict[str, SpeakerAssignment]] = {value: {} for value in ids}
    if not ids:
        return maps

    assignments = (
        SpeakerAssignment.query
        .filter(SpeakerAssignment.interview_id.in_(ids))
        .order_by(SpeakerAssignment.interview_id.asc(), SpeakerAssignment.id.asc())
        .all()
    )
    for assignment in assignments:
        label = str(assignment.speaker_label or "")
        if label:
            maps[int(assignment.interview_id)][label] = assignment
    return maps


def load_assignment_map(interview_id: int) -> dict[str, SpeakerAssignment]:
    """Load one interview's human speaker-assignment map."""
    return load_assignment_maps([int(interview_id)]).get(int(interview_id), {})


def effective_segment_role(
    segment,
    assignment_map: dict[str, SpeakerAssignment] | None = None,
) -> str:
    """Resolve the canonical role used by derived analysis and reporting.

    A human SpeakerAssignment is authoritative for its speaker label. Segment
    role remains a fallback for interviews/labels that have not been reviewed.
    Raw Segment text and stored transcription role are not rewritten.
    """
    assignment_map = assignment_map or {}
    assignment = assignment_map.get(str(getattr(segment, "speaker_label", "") or ""))
    if assignment is not None and assignment.speaker_role:
        return str(assignment.speaker_role)
    return str(getattr(segment, "speaker_role", None) or "unknown")


def effective_role_is(
    segment,
    role: str,
    assignment_map: dict[str, SpeakerAssignment] | None = None,
) -> bool:
    return effective_segment_role(segment, assignment_map) == role
