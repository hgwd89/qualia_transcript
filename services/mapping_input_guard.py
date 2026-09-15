from __future__ import annotations

from dataclasses import dataclass

from models import db
from models.segment import Segment, UtteranceMapping, UtteranceMappingProvenance
from services.mapping_source_provenance import (
    MappingSourceProvenanceError,
    build_mapping_source_manifest,
    mapping_source_provenance_status,
)


HUMAN_MAPPING_SOURCES = {"human", "manual"}


@dataclass(frozen=True)
class MappingInputStatus:
    current: bool
    reason: str
    respondent_segment_count: int
    mapping_count: int
    ai_mapping_count: int
    human_mapping_count: int


def _status(
    current: bool,
    reason: str,
    *,
    respondent_segment_count: int,
    mapping_count: int,
    ai_mapping_count: int,
    human_mapping_count: int,
) -> MappingInputStatus:
    return MappingInputStatus(
        current=bool(current),
        reason=str(reason or ""),
        respondent_segment_count=int(respondent_segment_count),
        mapping_count=int(mapping_count),
        ai_mapping_count=int(ai_mapping_count),
        human_mapping_count=int(human_mapping_count),
    )


def current_mapping_input_status(interview_id: int) -> MappingInputStatus:
    """Validate the exact mapping state that mapping-dependent analyses consume.

    Human/manual mappings are canonical human decisions and need no provider
    provenance. Every remaining AI mapping must belong to one coherent source
    generation that is still current. Every current respondent segment must have
    exactly one mapping row, including an explicit unclassified row when it does
    not map to a question.
    """
    interview_id = int(interview_id)
    try:
        manifest = build_mapping_source_manifest(interview_id)
    except ValueError as exc:
        return _status(
            False,
            str(exc),
            respondent_segment_count=0,
            mapping_count=0,
            ai_mapping_count=0,
            human_mapping_count=0,
        )

    expected_segment_ids = {int(row["id"]) for row in manifest["segments"]}
    allowed_question_ids = {int(row["id"]) for row in manifest["questions"]}
    if not expected_segment_ids:
        return _status(
            True,
            "",
            respondent_segment_count=0,
            mapping_count=0,
            ai_mapping_count=0,
            human_mapping_count=0,
        )

    rows = (
        db.session.query(UtteranceMapping, UtteranceMappingProvenance)
        .join(Segment, UtteranceMapping.segment_id == Segment.id)
        .outerjoin(
            UtteranceMappingProvenance,
            UtteranceMappingProvenance.mapping_id == UtteranceMapping.id,
        )
        .filter(
            Segment.interview_id == interview_id,
            Segment.speaker_role == "respondent",
        )
        .order_by(UtteranceMapping.id.asc())
        .all()
    )

    by_segment: dict[int, list[tuple[UtteranceMapping, UtteranceMappingProvenance | None]]] = {}
    for mapping, provenance in rows:
        by_segment.setdefault(int(mapping.segment_id), []).append((mapping, provenance))

    missing = expected_segment_ids - set(by_segment)
    if missing:
        rendered = ", ".join(str(value) for value in sorted(missing))
        return _status(
            False,
            f"mapping input does not cover respondent segment_id(s): {rendered}",
            respondent_segment_count=len(expected_segment_ids),
            mapping_count=len(rows),
            ai_mapping_count=0,
            human_mapping_count=0,
        )

    duplicates = sorted(segment_id for segment_id, items in by_segment.items() if len(items) != 1)
    if duplicates:
        rendered = ", ".join(str(value) for value in duplicates)
        return _status(
            False,
            f"mapping input has duplicate rows for respondent segment_id(s): {rendered}",
            respondent_segment_count=len(expected_segment_ids),
            mapping_count=len(rows),
            ai_mapping_count=0,
            human_mapping_count=0,
        )

    ai_proofs: set[str | None] = set()
    ai_count = 0
    human_count = 0
    for segment_id in sorted(expected_segment_ids):
        mapping, provenance = by_segment[segment_id][0]
        if mapping.question_id is not None and int(mapping.question_id) not in allowed_question_ids:
            return _status(
                False,
                f"mapping question_id={mapping.question_id} is outside the interview flow",
                respondent_segment_count=len(expected_segment_ids),
                mapping_count=len(rows),
                ai_mapping_count=ai_count,
                human_mapping_count=human_count,
            )

        mapped_by = str(mapping.mapped_by or "ai").strip().lower()
        if mapped_by in HUMAN_MAPPING_SOURCES:
            human_count += 1
            continue
        if mapped_by != "ai":
            return _status(
                False,
                f"mapping has unsupported mapped_by value: {mapped_by or '<empty>'}",
                respondent_segment_count=len(expected_segment_ids),
                mapping_count=len(rows),
                ai_mapping_count=ai_count,
                human_mapping_count=human_count,
            )

        ai_count += 1
        ai_proofs.add(
            provenance.source_provenance_json if provenance is not None else None
        )

    if ai_count:
        if None in ai_proofs:
            return _status(
                False,
                "AI mapping source provenance is missing",
                respondent_segment_count=len(expected_segment_ids),
                mapping_count=len(rows),
                ai_mapping_count=ai_count,
                human_mapping_count=human_count,
            )
        if len(ai_proofs) != 1:
            return _status(
                False,
                "AI mapping rows contain mixed source generations",
                respondent_segment_count=len(expected_segment_ids),
                mapping_count=len(rows),
                ai_mapping_count=ai_count,
                human_mapping_count=human_count,
            )
        proof = next(iter(ai_proofs))
        current, reason = mapping_source_provenance_status(
            proof,
            interview_id=interview_id,
        )
        if not current:
            return _status(
                False,
                reason,
                respondent_segment_count=len(expected_segment_ids),
                mapping_count=len(rows),
                ai_mapping_count=ai_count,
                human_mapping_count=human_count,
            )

    return _status(
        True,
        "",
        respondent_segment_count=len(expected_segment_ids),
        mapping_count=len(rows),
        ai_mapping_count=ai_count,
        human_mapping_count=human_count,
    )


def require_current_mapping_input(interview_id: int) -> MappingInputStatus:
    status = current_mapping_input_status(interview_id)
    if not status.current:
        raise MappingSourceProvenanceError(status.reason)
    return status
