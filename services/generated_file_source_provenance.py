"""Generation-time source provenance for ordinary generated deliverables.

Verbatim, formatted-sheet, and flat-analysis exports are professional research
artifacts derived from mutable canonical rows.  This module records the exact
canonical source state used to generate them so registration, download, and
readiness can reject stale or mixed-snapshot outputs.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from models import db
from models.interview import Interview
from models.interview_flow import InterviewFlowQuestion
from models.participant import Participant
from models.project import Project
from models.segment import Segment, UtteranceMapping
from models.segment_flag import SegmentFlag
from models.speaker_assignment import SpeakerAssignment


PROVENANCE_KEY = "source_provenance"
PROVENANCE_VERSION = "generated-file-input-v1"
SUPPORTED_FILE_TYPES = {"verbatim", "formatted_sheet", "analysis"}


class GeneratedFileSourceProvenanceError(ValueError):
    """A generated deliverable's canonical source state cannot be proven."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(manifest: dict) -> str:
    return hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _project(project_id: int) -> Project:
    project = db.session.get(Project, int(project_id))
    if project is None:
        raise GeneratedFileSourceProvenanceError("project source row is missing")
    return project


def _participant_identity(participant: Participant) -> dict:
    return {
        "id": int(participant.id),
        "participant_code": str(participant.participant_code or ""),
        "display_name": str(participant.display_name or ""),
    }


def _interview_identity(interview: Interview) -> dict:
    return {
        "id": int(interview.id),
        "participant_id": int(interview.participant_id) if interview.participant_id is not None else None,
        "flow_id": int(interview.flow_id) if interview.flow_id is not None else None,
        "interview_date": interview.interview_date.isoformat() if interview.interview_date else None,
        "interviewer_name": str(interview.interviewer_name or ""),
    }


def _segment_core(segment: Segment) -> dict:
    return {
        "id": int(segment.id),
        "interview_id": int(segment.interview_id),
        "participant_id": int(segment.participant_id) if segment.participant_id is not None else None,
        "speaker_label": str(segment.speaker_label or ""),
        "speaker_role": str(segment.speaker_role or ""),
        "seq": int(segment.seq),
        "start_sec": segment.start_sec,
        "end_sec": segment.end_sec,
        "text": str(segment.text),
    }


def _verbatim_manifest(project_id: int, interview_id: int | None) -> dict:
    if interview_id is None:
        raise GeneratedFileSourceProvenanceError("verbatim requires interview_id")
    project = _project(project_id)
    interview = db.session.get(Interview, int(interview_id))
    if interview is None or int(interview.project_id) != int(project_id):
        raise GeneratedFileSourceProvenanceError("verbatim interview is missing or cross-project")

    participant_ids = {
        int(value)
        for value in [interview.participant_id]
        if value is not None
    }
    segments = (
        Segment.query
        .filter_by(interview_id=int(interview_id))
        .order_by(Segment.seq.asc(), Segment.id.asc())
        .all()
    )
    for segment in segments:
        if segment.participant_id is not None:
            participant_ids.add(int(segment.participant_id))

    assignments = (
        SpeakerAssignment.query
        .filter_by(interview_id=int(interview_id))
        .order_by(SpeakerAssignment.speaker_label.asc(), SpeakerAssignment.id.asc())
        .all()
    )
    for assignment in assignments:
        if assignment.participant_id is not None:
            participant_ids.add(int(assignment.participant_id))

    participants = (
        Participant.query
        .filter(Participant.id.in_(sorted(participant_ids)))
        .order_by(Participant.id.asc())
        .all()
        if participant_ids
        else []
    )
    flags = (
        SegmentFlag.query
        .join(Segment, Segment.id == SegmentFlag.segment_id)
        .filter(
            Segment.interview_id == int(interview_id),
            SegmentFlag.flag_type == "quote",
        )
        .order_by(SegmentFlag.segment_id.asc(), SegmentFlag.id.asc())
        .all()
    )

    return {
        "version": PROVENANCE_VERSION,
        "file_type": "verbatim",
        "project": {
            "id": int(project.id),
            "name": str(project.name or ""),
            "client": str(project.client or ""),
        },
        "interview": _interview_identity(interview),
        "participants": [_participant_identity(row) for row in participants],
        "speaker_assignments": [
            {
                "id": int(row.id),
                "speaker_label": str(row.speaker_label or ""),
                "speaker_role": str(row.speaker_role or ""),
                "participant_id": int(row.participant_id) if row.participant_id is not None else None,
            }
            for row in assignments
        ],
        "segments": [_segment_core(row) for row in segments],
        "quote_flags": [
            {"segment_id": int(row.segment_id), "flag_type": "quote"}
            for row in flags
        ],
    }


def _project_interviews(project_id: int) -> list[Interview]:
    return (
        Interview.query
        .filter_by(project_id=int(project_id))
        .order_by(Interview.id.asc())
        .all()
    )


def _project_participants(project_id: int) -> list[Participant]:
    return (
        Participant.query
        .filter_by(project_id=int(project_id))
        .order_by(Participant.id.asc())
        .all()
    )


def _formatted_manifest(project_id: int) -> dict:
    project = _project(project_id)
    interviews = _project_interviews(project_id)
    interview_ids = [int(row.id) for row in interviews]
    participants = _project_participants(project_id)

    segments = (
        Segment.query
        .filter(Segment.interview_id.in_(interview_ids))
        .order_by(Segment.interview_id.asc(), Segment.seq.asc(), Segment.id.asc())
        .all()
        if interview_ids
        else []
    )
    segment_ids = [int(row.id) for row in segments]
    mappings = (
        UtteranceMapping.query
        .filter(UtteranceMapping.segment_id.in_(segment_ids))
        .order_by(UtteranceMapping.segment_id.asc(), UtteranceMapping.id.asc())
        .all()
        if segment_ids
        else []
    )
    assignments = (
        SpeakerAssignment.query
        .filter(SpeakerAssignment.interview_id.in_(interview_ids))
        .order_by(SpeakerAssignment.interview_id.asc(), SpeakerAssignment.speaker_label.asc(), SpeakerAssignment.id.asc())
        .all()
        if interview_ids
        else []
    )
    flags = (
        SegmentFlag.query
        .filter(
            SegmentFlag.segment_id.in_(segment_ids),
            SegmentFlag.flag_type.in_(("favorite", "quote", "exclude", "needs_review")),
        )
        .order_by(SegmentFlag.segment_id.asc(), SegmentFlag.flag_type.asc(), SegmentFlag.id.asc())
        .all()
        if segment_ids
        else []
    )

    flows = sorted(project.interview_flows, key=lambda row: int(row.id))
    flow_rows = []
    for flow in flows:
        sections = []
        for section in sorted(flow.sections, key=lambda row: (int(row.seq), int(row.id))):
            questions = []
            for question in sorted(section.questions, key=lambda row: (int(row.seq), int(row.id))):
                questions.append({
                    "id": int(question.id),
                    "question_code": str(question.question_code or ""),
                    "question_text": str(question.question_text or ""),
                    "is_key_question": bool(question.is_key_question),
                    "seq": int(question.seq),
                })
            sections.append({
                "id": int(section.id),
                "title": str(section.title or ""),
                "seq": int(section.seq),
                "questions": questions,
            })
        flow_rows.append({
            "id": int(flow.id),
            "title": str(flow.title or ""),
            "sections": sections,
        })

    return {
        "version": PROVENANCE_VERSION,
        "file_type": "formatted_sheet",
        "project": {"id": int(project.id), "name": str(project.name or "")},
        "interviews": [_interview_identity(row) for row in interviews],
        "participants": [_participant_identity(row) for row in participants],
        "flows": flow_rows,
        "segments": [_segment_core(row) for row in segments],
        "mappings": [
            {
                "id": int(row.id),
                "segment_id": int(row.segment_id),
                "question_id": int(row.question_id) if row.question_id is not None else None,
                "is_unclassified": bool(row.is_unclassified),
            }
            for row in mappings
        ],
        "speaker_assignments": [
            {
                "id": int(row.id),
                "interview_id": int(row.interview_id),
                "speaker_label": str(row.speaker_label or ""),
                "speaker_role": str(row.speaker_role or ""),
                "participant_id": int(row.participant_id) if row.participant_id is not None else None,
            }
            for row in assignments
        ],
        "flags": [
            {"segment_id": int(row.segment_id), "flag_type": str(row.flag_type)}
            for row in flags
        ],
    }


def _analysis_manifest(project_id: int) -> dict:
    project = _project(project_id)
    interviews = _project_interviews(project_id)
    interview_ids = [int(row.id) for row in interviews]
    participants = _project_participants(project_id)

    segments = (
        Segment.query
        .filter(Segment.interview_id.in_(interview_ids))
        .order_by(Segment.interview_id.asc(), Segment.seq.asc(), Segment.id.asc())
        .all()
        if interview_ids
        else []
    )
    segment_ids = [int(row.id) for row in segments]
    mappings = (
        UtteranceMapping.query
        .filter(UtteranceMapping.segment_id.in_(segment_ids))
        .order_by(UtteranceMapping.segment_id.asc(), UtteranceMapping.id.asc())
        .all()
        if segment_ids
        else []
    )
    question_ids = sorted({
        int(row.question_id)
        for row in mappings
        if row.question_id is not None
    })
    questions = (
        InterviewFlowQuestion.query
        .filter(InterviewFlowQuestion.id.in_(question_ids))
        .order_by(InterviewFlowQuestion.id.asc())
        .all()
        if question_ids
        else []
    )

    participant_rows = []
    for participant in participants:
        participant_rows.append({
            **_participant_identity(participant),
            "attributes": [
                {
                    "id": int(attribute.id),
                    "attribute_key": str(attribute.attribute_key or ""),
                    "attribute_value": str(attribute.attribute_value or ""),
                    "display_order": int(attribute.display_order or 0),
                }
                for attribute in sorted(
                    participant.attributes,
                    key=lambda row: (int(row.display_order or 0), int(row.id)),
                )
            ],
        })

    question_rows = []
    for question in questions:
        section = question.section
        question_rows.append({
            "id": int(question.id),
            "section_id": int(question.section_id),
            "section_title": str(section.title or "") if section else "",
            "question_code": str(question.question_code or ""),
            "question_text": str(question.question_text or ""),
            "is_key_question": bool(question.is_key_question),
        })

    return {
        "version": PROVENANCE_VERSION,
        "file_type": "analysis",
        "project": {"id": int(project.id), "name": str(project.name or "")},
        "interviews": [_interview_identity(row) for row in interviews],
        "participants": participant_rows,
        "segments": [_segment_core(row) for row in segments],
        "mappings": [
            {
                "id": int(row.id),
                "segment_id": int(row.segment_id),
                "question_id": int(row.question_id) if row.question_id is not None else None,
                "mapped_by": str(row.mapped_by or ""),
                "confidence": row.confidence,
                "is_unclassified": bool(row.is_unclassified),
            }
            for row in mappings
        ],
        "questions": question_rows,
    }


def build_generated_file_source_manifest(
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> dict:
    file_type = str(file_type or "")
    if file_type == "verbatim":
        return _verbatim_manifest(project_id, interview_id)
    if file_type == "formatted_sheet":
        return _formatted_manifest(project_id)
    if file_type == "analysis":
        return _analysis_manifest(project_id)
    raise GeneratedFileSourceProvenanceError(
        f"unsupported generated file_type for source provenance: {file_type}"
    )


def capture_generated_file_source_provenance(
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> dict:
    manifest = build_generated_file_source_manifest(
        file_type,
        project_id,
        interview_id=interview_id,
    )
    return {
        "version": PROVENANCE_VERSION,
        "sha256": _fingerprint(manifest),
    }


def source_provenance_matches_scope(
    expected: dict,
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> tuple[bool, str]:
    if not isinstance(expected, dict):
        return False, "generated-file source provenance is missing"
    if expected.get("version") != PROVENANCE_VERSION:
        return False, "generated-file source provenance version is missing or unsupported"
    expected_hash = str(expected.get("sha256") or "").strip().lower()
    if len(expected_hash) != 64 or any(ch not in "0123456789abcdef" for ch in expected_hash):
        return False, "generated-file source provenance hash is missing or invalid"
    try:
        current = capture_generated_file_source_provenance(
            file_type,
            project_id,
            interview_id=interview_id,
        )
    except GeneratedFileSourceProvenanceError as exc:
        return False, str(exc)
    if current["sha256"] != expected_hash:
        return False, "canonical generated-file inputs changed after generation"
    return True, ""


def require_current_generated_file_source_provenance(
    expected: dict,
    file_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
) -> None:
    ok, reason = source_provenance_matches_scope(
        expected,
        file_type,
        project_id,
        interview_id=interview_id,
    )
    if not ok:
        raise GeneratedFileSourceProvenanceError(reason)
