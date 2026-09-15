from __future__ import annotations

import hashlib
import json
from typing import Any

from models import db
from models.interview import Interview
from models.interview_flow import InterviewFlowQuestion, InterviewFlowSection
from models.segment import Segment, UtteranceMapping, UtteranceMappingProvenance

MAPPING_PROVENANCE_VERSION = "mapping-input-v1"


class MappingSourceProvenanceError(RuntimeError):
    """Canonical mapping inputs changed after provider generation began."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def build_mapping_source_manifest(interview_id: int) -> dict[str, Any]:
    """Return the exact canonical input consumed by AI utterance mapping."""
    interview = db.session.get(Interview, int(interview_id))
    if interview is None:
        raise ValueError("interview not found")
    if interview.flow_id is None:
        raise ValueError("interview flow is not set")

    segments = (
        Segment.query
        .filter_by(interview_id=int(interview.id), speaker_role="respondent")
        .order_by(Segment.seq.asc(), Segment.id.asc())
        .all()
    )
    questions = (
        db.session.query(InterviewFlowQuestion, InterviewFlowSection)
        .join(
            InterviewFlowSection,
            InterviewFlowQuestion.section_id == InterviewFlowSection.id,
        )
        .filter(InterviewFlowSection.flow_id == int(interview.flow_id))
        .order_by(
            InterviewFlowSection.seq.asc(),
            InterviewFlowSection.id.asc(),
            InterviewFlowQuestion.seq.asc(),
            InterviewFlowQuestion.id.asc(),
        )
        .all()
    )

    return {
        "version": MAPPING_PROVENANCE_VERSION,
        "project_id": int(interview.project_id),
        "interview_id": int(interview.id),
        "flow_id": int(interview.flow_id),
        "segments": [
            {
                "id": int(segment.id),
                "seq": int(segment.seq),
                "speaker_role": str(segment.speaker_role or ""),
                "text": str(segment.text or ""),
            }
            for segment in segments
        ],
        "questions": [
            {
                "section_id": int(section.id),
                "section_seq": int(section.seq),
                "id": int(question.id),
                "seq": int(question.seq),
                "question_code": question.question_code,
                "question_text": str(question.question_text or ""),
            }
            for question, section in questions
        ],
    }


def mapping_source_provenance_for_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": MAPPING_PROVENANCE_VERSION,
        "project_id": int(manifest["project_id"]),
        "interview_id": int(manifest["interview_id"]),
        "flow_id": int(manifest["flow_id"]),
        "sha256": _sha256(manifest),
    }


def capture_mapping_source_provenance(interview_id: int) -> dict[str, Any]:
    return mapping_source_provenance_for_manifest(
        build_mapping_source_manifest(interview_id)
    )


def serialize_mapping_source_provenance(provenance: dict[str, Any]) -> str:
    return _canonical_json(provenance)


def parse_mapping_source_provenance(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def mapping_source_provenance_status(
    provenance: dict[str, Any] | str | None,
    *,
    interview_id: int | None = None,
) -> tuple[bool, str]:
    """Return whether one persisted mapping proof matches current canonical input."""
    if isinstance(provenance, str) or provenance is None:
        parsed = parse_mapping_source_provenance(provenance)
    else:
        parsed = provenance
    if not isinstance(parsed, dict):
        return False, "mapping source provenance is missing or malformed"
    if parsed.get("version") != MAPPING_PROVENANCE_VERSION:
        return False, "mapping source provenance version is unsupported"

    try:
        stored_interview_id = int(parsed["interview_id"])
        stored_project_id = int(parsed["project_id"])
        stored_flow_id = int(parsed["flow_id"])
        stored_hash = str(parsed["sha256"])
    except (KeyError, TypeError, ValueError):
        return False, "mapping source provenance is incomplete"

    if interview_id is not None and stored_interview_id != int(interview_id):
        return False, "mapping source provenance interview scope does not match"

    try:
        current_manifest = build_mapping_source_manifest(stored_interview_id)
    except ValueError as exc:
        return False, str(exc)

    if int(current_manifest["project_id"]) != stored_project_id:
        return False, "mapping source provenance project scope does not match"
    if int(current_manifest["flow_id"]) != stored_flow_id:
        return False, "mapping source provenance flow scope does not match"
    if _sha256(current_manifest) != stored_hash:
        return False, "mapping canonical source changed after generation"
    return True, ""


def validate_current_ai_mapping_batch(interview_id: int) -> tuple[bool, str, int]:
    """Validate all current AI mappings as one coherent, current generation."""
    rows = (
        db.session.query(UtteranceMapping, UtteranceMappingProvenance)
        .join(Segment, UtteranceMapping.segment_id == Segment.id)
        .outerjoin(
            UtteranceMappingProvenance,
            UtteranceMappingProvenance.mapping_id == UtteranceMapping.id,
        )
        .filter(
            Segment.interview_id == int(interview_id),
            Segment.speaker_role == "respondent",
            UtteranceMapping.mapped_by == "ai",
        )
        .order_by(UtteranceMapping.id.asc())
        .all()
    )
    if not rows:
        return False, "current interview has no AI mapping generation", 0

    proofs = {
        provenance.source_provenance_json if provenance is not None else None
        for _mapping, provenance in rows
    }
    if len(proofs) != 1:
        return False, "AI mapping generation has mixed source provenance", len(rows)

    proof = next(iter(proofs))
    current, reason = mapping_source_provenance_status(
        proof,
        interview_id=int(interview_id),
    )
    if not current:
        return False, reason, len(rows)

    manifest = build_mapping_source_manifest(int(interview_id))
    expected_segment_ids = {int(row["id"]) for row in manifest["segments"]}
    mapped_segment_ids = {int(mapping.segment_id) for mapping, _provenance in rows}
    if mapped_segment_ids != expected_segment_ids:
        return False, "AI mapping generation does not cover the current respondent set", len(rows)

    return True, "", len(rows)
