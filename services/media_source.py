"""Deterministic source-media selection for transcription workflows."""
from __future__ import annotations

from models.interview import MediaFile


def canonical_media_for_interview(interview_id: int) -> MediaFile | None:
    """Return the latest registered MediaFile deterministically.

    Historical/restored databases may contain more than one MediaFile for an
    Interview. SQLAlchemy relationship collection order is not a source identity
    contract, so callers must not use ``interview.media_files[-1]``. The highest
    MediaFile primary key is the canonical latest registration generation.
    """
    return (
        MediaFile.query
        .filter_by(interview_id=int(interview_id))
        .order_by(MediaFile.id.desc())
        .first()
    )
