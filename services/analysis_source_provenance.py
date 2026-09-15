"""Generation-time source provenance for persisted AI analyses.

The durable-job lease prevents canonical research inputs from changing while a
production analysis worker is running. This module adds the complementary
long-lived contract: a saved analysis records a fingerprint of the canonical
inputs that produced it, and later approval/formal export can prove those inputs
still match the current database state.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from models import db
from models.analysis import AIAnalysis
from models.interview import Interview
from models.interview_flow import InterviewFlowQuestion
from models.project import Project
from models.segment import Segment, UtteranceMapping
from services.project_flow_scope import resolve_integrated_analysis_scope

PROVENANCE_KEY = "source_provenance"
PROVENANCE_VERSION = "analysis-input-v1"
SUPPORTED_ANALYSIS_TYPES = {
    "per_question",
    "per_participant",
    "cross_participant",
    "integrated",
}


class AnalysisSourceProvenanceError(ValueError):
    """The source state needed to validate one analysis cannot be proven."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(manifest: dict) -> str:
    return hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _require_supported_project_participant_model(project: Project) -> None:
    """Refuse persisted AI analysis while FGI attribution is not provenance-safe.

    Persisted analyzer prompts currently identify respondent material through the
    interview-level participant. FGI permits multiple participant-attributed
    speakers inside one interview, which cannot be represented faithfully by the
    current analysis-input-v1 manifest. Failing before provider work is safer than
    silently assigning every respondent utterance to one Interview.participant.
    """
    if str(project.method or "DI").strip().upper() == "FGI":
        raise AnalysisSourceProvenanceError(
            "FGI persisted AI analysis is not supported by the current participant provenance model"
        )


def _require_supported_interview_participant_model(interview: Interview) -> None:
    """Require respondent segment attribution to agree with Interview.participant."""
    project = interview.project
    if project is None:
        project = db.session.get(Project, int(interview.project_id))
    if project is None:
        raise AnalysisSourceProvenanceError("analysis project is missing")
    _require_supported_project_participant_model(project)

    expected_participant_id = (
        int(interview.participant_id)
        if interview.participant_id is not None
        else None
    )
    respondent_segments = (
        Segment.query
        .filter_by(interview_id=int(interview.id), speaker_role="respondent")
        .order_by(Segment.id.asc())
        .all()
    )
    mismatched_segment_ids = [
        int(segment.id)
        for segment in respondent_segments
        if segment.participant_id is not None
        and (
            expected_participant_id is None
            or int(segment.participant_id) != expected_participant_id
        )
    ]
    if mismatched_segment_ids:
        rendered = ", ".join(str(value) for value in mismatched_segment_ids[:20])
        suffix = " ..." if len(mismatched_segment_ids) > 20 else ""
        raise AnalysisSourceProvenanceError(
            "respondent segment participant attribution differs from Interview.participant "
            f"for interview_id={int(interview.id)} segment_id(s): {rendered}{suffix}"
        )


def _segment_manifest(segment: Segment) -> dict:
    return {
        "id": int(segment.id),
        "seq": int(segment.seq),
        "text": str(segment.text),
    }


def _question_manifest(question: InterviewFlowQuestion) -> dict:
    section = question.section
    if section is None or section.flow is None:
        raise AnalysisSourceProvenanceError("question flow cannot be resolved")
    return {
        "id": int(question.id),
        "flow_id": int(section.flow_id),
        "section_id": int(section.id),
        "question_code": str(question.question_code or ""),
        "question_text": str(question.question_text or ""),
        "seq": int(question.seq),
    }


def _mapped_respondent_segments(interview_id: int, question_id: int) -> list[Segment]:
    # Mapping-dependent analyses must never consume an AI classification whose
    # generation proof is stale or missing. Import locally to avoid a module
    # cycle: mapping_input_guard itself reuses mapping source-provenance helpers.
    from services.mapping_input_guard import require_current_mapping_input
    from services.mapping_source_provenance import MappingSourceProvenanceError

    try:
        require_current_mapping_input(int(interview_id))
    except MappingSourceProvenanceError as exc:
        raise AnalysisSourceProvenanceError(
            f"mapping input is stale or unprovable: {exc}"
        ) from exc

    rows = (
        Segment.query
        .join(UtteranceMapping, UtteranceMapping.segment_id == Segment.id)
        .filter(
            Segment.interview_id == int(interview_id),
            Segment.speaker_role == "respondent",
            UtteranceMapping.question_id == int(question_id),
        )
        .order_by(Segment.seq.asc(), Segment.id.asc())
        .all()
    )
    # Historical duplicate mappings must not make the source fingerprint depend
    # on duplicate join rows when the provider prompt only contains the utterance.
    by_id = {int(row.id): row for row in rows}
    return sorted(by_id.values(), key=lambda row: (int(row.seq), int(row.id)))


def _per_question_manifest(project_id: int, interview_id: int | None, question_id: int | None) -> dict:
    if interview_id is None or question_id is None:
        raise AnalysisSourceProvenanceError("per_question requires interview_id and question_id")
    interview = db.session.get(Interview, int(interview_id))
    question = db.session.get(InterviewFlowQuestion, int(question_id))
    if interview is None or question is None:
        raise AnalysisSourceProvenanceError("per_question source row is missing")
    if int(interview.project_id) != int(project_id):
        raise AnalysisSourceProvenanceError("interview belongs to another project")
    _require_supported_interview_participant_model(interview)
    q_manifest = _question_manifest(question)
    if interview.flow_id is None or int(interview.flow_id) != int(q_manifest["flow_id"]):
        raise AnalysisSourceProvenanceError("question is outside the interview flow")

    participant = interview.participant
    participant_code = participant.participant_code if participant else "P??"
    return {
        "version": PROVENANCE_VERSION,
        "analysis_type": "per_question",
        "project_id": int(project_id),
        "interview": {
            "id": int(interview.id),
            "flow_id": int(interview.flow_id),
            "participant_id": int(interview.participant_id) if interview.participant_id is not None else None,
            "participant_code": str(participant_code or "P??"),
        },
        "question": q_manifest,
        "segments": [
            _segment_manifest(segment)
            for segment in _mapped_respondent_segments(interview.id, question.id)
        ],
    }


def _per_participant_manifest(project_id: int, interview_id: int | None) -> dict:
    if interview_id is None:
        raise AnalysisSourceProvenanceError("per_participant requires interview_id")
    interview = db.session.get(Interview, int(interview_id))
    if interview is None or int(interview.project_id) != int(project_id):
        raise AnalysisSourceProvenanceError("participant analysis interview is missing or cross-project")
    _require_supported_interview_participant_model(interview)

    participant = interview.participant
    code = participant.participant_code if participant else "P??"
    display_name = participant.display_name if participant and participant.display_name else code
    segments = (
        Segment.query
        .filter_by(interview_id=interview.id, speaker_role="respondent")
        .order_by(Segment.seq.asc(), Segment.id.asc())
        .all()
    )
    return {
        "version": PROVENANCE_VERSION,
        "analysis_type": "per_participant",
        "project_id": int(project_id),
        "interview": {
            "id": int(interview.id),
            "flow_id": int(interview.flow_id) if interview.flow_id is not None else None,
            "participant_id": int(interview.participant_id) if interview.participant_id is not None else None,
            "participant_code": str(code or "P??"),
            "participant_display_name": str(display_name or "参加者未設定"),
        },
        "segments": [_segment_manifest(segment) for segment in segments],
    }


def _cross_participant_manifest(project_id: int, question_id: int | None) -> dict:
    if question_id is None:
        raise AnalysisSourceProvenanceError("cross_participant requires question_id")
    project = db.session.get(Project, int(project_id))
    question = db.session.get(InterviewFlowQuestion, int(question_id))
    if project is None or question is None:
        raise AnalysisSourceProvenanceError("cross-participant source row is missing")
    _require_supported_project_participant_model(project)
    q_manifest = _question_manifest(question)
    if question.section.flow.project_id != int(project_id):
        raise AnalysisSourceProvenanceError("cross-participant question belongs to another project")

    participants = []
    for interview in sorted(project.interviews, key=lambda row: int(row.id)):
        if interview.flow_id is None or int(interview.flow_id) != int(q_manifest["flow_id"]):
            continue
        _require_supported_interview_participant_model(interview)
        participant = interview.participant
        if participant is None:
            continue
        segments = _mapped_respondent_segments(interview.id, question.id)
        if not segments:
            continue
        participants.append({
            "interview_id": int(interview.id),
            "participant_id": int(participant.id),
            "participant_code": str(participant.participant_code or ""),
            "segments": [_segment_manifest(segment) for segment in segments],
        })

    if not participants:
        raise AnalysisSourceProvenanceError("cross-participant analysis has no mapped respondent source")
    return {
        "version": PROVENANCE_VERSION,
        "analysis_type": "cross_participant",
        "project_id": int(project_id),
        "question": q_manifest,
        "participants": participants,
    }


def _integrated_manifest(project_id: int) -> dict:
    project = db.session.get(Project, int(project_id))
    if project is None:
        raise AnalysisSourceProvenanceError("integrated project is missing")
    _require_supported_project_participant_model(project)

    try:
        scope = resolve_integrated_analysis_scope(project)
    except Exception as exc:
        raise AnalysisSourceProvenanceError(f"integrated source scope is invalid: {exc}") from exc

    source_interview_ids = {int(value) for value in scope.interview_ids}
    interview_rows = []
    for interview in sorted(project.interviews, key=lambda row: int(row.id)):
        if int(interview.id) not in source_interview_ids:
            continue
        _require_supported_interview_participant_model(interview)
        participant = interview.participant
        interview_rows.append({
            "id": int(interview.id),
            "participant_id": int(participant.id) if participant else None,
            "participant_code": str(participant.participant_code or "") if participant else "",
        })

    sections = []
    for section in sorted(scope.flow.sections, key=lambda row: (int(row.seq), int(row.id))):
        questions = []
        for question in sorted(section.questions, key=lambda row: (int(row.seq), int(row.id))):
            sources = []
            for interview in sorted(project.interviews, key=lambda row: int(row.id)):
                if int(interview.id) not in source_interview_ids or interview.participant is None:
                    continue
                segments = _mapped_respondent_segments(interview.id, question.id)
                if not segments:
                    continue
                sources.append({
                    "interview_id": int(interview.id),
                    "participant_id": int(interview.participant.id),
                    "participant_code": str(interview.participant.participant_code or ""),
                    "segments": [_segment_manifest(segment) for segment in segments],
                })
            questions.append({
                "id": int(question.id),
                "question_code": str(question.question_code or ""),
                "question_text": str(question.question_text or ""),
                "seq": int(question.seq),
                "sources": sources,
            })
        sections.append({
            "id": int(section.id),
            "title": str(section.title or ""),
            "seq": int(section.seq),
            "questions": questions,
        })

    return {
        "version": PROVENANCE_VERSION,
        "analysis_type": "integrated",
        "project": {
            "id": int(project.id),
            "name": str(project.name or ""),
            "client": str(project.client or ""),
            "research_objective": str(project.research_objective or ""),
        },
        "flow_id": int(scope.flow.id),
        "source_interviews": interview_rows,
        "sections": sections,
    }


def build_analysis_source_manifest(
    analysis_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
    question_id: int | None = None,
) -> dict:
    analysis_type = str(analysis_type or "")
    if analysis_type == "per_question":
        return _per_question_manifest(project_id, interview_id, question_id)
    if analysis_type == "per_participant":
        return _per_participant_manifest(project_id, interview_id)
    if analysis_type == "cross_participant":
        return _cross_participant_manifest(project_id, question_id)
    if analysis_type == "integrated":
        return _integrated_manifest(project_id)
    raise AnalysisSourceProvenanceError(
        f"unsupported analysis_type for source provenance: {analysis_type}"
    )


def capture_analysis_source_provenance(
    analysis_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
    question_id: int | None = None,
) -> dict:
    manifest = build_analysis_source_manifest(
        analysis_type,
        project_id,
        interview_id=interview_id,
        question_id=question_id,
    )
    return {
        "version": PROVENANCE_VERSION,
        "sha256": _fingerprint(manifest),
    }


def source_provenance_matches_scope(
    expected: dict,
    analysis_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
    question_id: int | None = None,
) -> tuple[bool, str]:
    if not isinstance(expected, dict):
        return False, "source provenance is missing"
    if expected.get("version") != PROVENANCE_VERSION:
        return False, "source provenance version is missing or unsupported"
    expected_hash = str(expected.get("sha256") or "")
    if not expected_hash:
        return False, "source provenance hash is missing"
    try:
        current = capture_analysis_source_provenance(
            analysis_type,
            project_id,
            interview_id=interview_id,
            question_id=question_id,
        )
    except AnalysisSourceProvenanceError as exc:
        return False, str(exc)
    if current["sha256"] != expected_hash:
        return False, "canonical analysis inputs changed after generation"
    return True, ""


def analysis_source_provenance_status(analysis: AIAnalysis) -> tuple[bool, str]:
    if analysis.analysis_type not in SUPPORTED_ANALYSIS_TYPES:
        return False, f"unsupported analysis_type for formal approval: {analysis.analysis_type}"
    try:
        content = json.loads(analysis.content_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return False, "analysis content_json is invalid"
    if not isinstance(content, dict):
        return False, "analysis content_json is not an object"
    return source_provenance_matches_scope(
        content.get(PROVENANCE_KEY),
        analysis.analysis_type,
        int(analysis.project_id),
        interview_id=int(analysis.interview_id) if analysis.interview_id is not None else None,
        question_id=int(analysis.question_id) if analysis.question_id is not None else None,
    )


def require_current_analysis_source_provenance(analysis: AIAnalysis) -> None:
    ok, reason = analysis_source_provenance_status(analysis)
    if not ok:
        raise AnalysisSourceProvenanceError(reason)
