"""Human review and source-traceability helpers for AIAnalysis."""
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text

from models import db
from models.analysis import AIAnalysis
from models.interview import Interview
from models.segment import Segment
from services.analysis_source_provenance import (
    AnalysisSourceProvenanceError,
    require_current_analysis_source_provenance,
)


ALLOWED_REVIEW_STATUSES = {"draft", "approved", "rejected"}


def _normalize_text(value: str | None) -> str:
    text = str(value or "").strip()
    if "「" in text and "」" in text and text.find("「") < text.rfind("」"):
        text = text[text.find("「") + 1:text.rfind("」")]
    text = text.strip(' \t\r\n"\'“”‘’「」『』')
    return re.sub(r"\s+", " ", text).strip()


def _analysis_content(analysis: AIAnalysis) -> dict:
    try:
        payload = json.loads(analysis.content_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _stored_source_flow_id(analysis: AIAnalysis) -> int | None:
    raw = _analysis_content(analysis).get("source_flow_id")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _stored_source_interview_ids(analysis: AIAnalysis) -> set[int] | None:
    content = _analysis_content(analysis)
    if "source_interview_ids" not in content:
        return None
    raw_values = content.get("source_interview_ids")
    if not isinstance(raw_values, list):
        return set()

    values: set[int] = set()
    for raw in raw_values:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.add(value)
    return values


def _analysis_source_flow_id(analysis: AIAnalysis) -> int | None:
    stored = _stored_source_flow_id(analysis)
    if stored is not None:
        return stored

    if (
        analysis.analysis_type == "integrated"
        and analysis.interview_id is None
        and analysis.question_id is None
        and analysis.project is not None
    ):
        flow_ids = [int(flow.id) for flow in analysis.project.interview_flows]
        if len(flow_ids) == 1:
            return flow_ids[0]
    return None


def _integrated_scope_is_ambiguous(analysis: AIAnalysis) -> bool:
    if (
        analysis.analysis_type != "integrated"
        or analysis.interview_id is not None
        or analysis.question_id is not None
        or analysis.project is None
        or _stored_source_flow_id(analysis) is not None
    ):
        return False
    return len(analysis.project.interview_flows) > 1


def _segment_question_codes(segment: Segment, *, flow_id: int | None = None) -> set[str]:
    codes = set()
    for mapping in segment.utterance_mappings:
        question = mapping.question
        if not question or not question.question_code:
            continue
        if flow_id is not None:
            section = question.section
            if not section or int(section.flow_id) != int(flow_id):
                continue
        codes.add(str(question.question_code))
    return codes


def _segment_has_question_id(segment: Segment, question_id: int) -> bool:
    return any(
        mapping.question_id is not None and int(mapping.question_id) == int(question_id)
        for mapping in segment.utterance_mappings
    )


def _segment_has_flow_mapping(segment: Segment, flow_id: int) -> bool:
    for mapping in segment.utterance_mappings:
        question = mapping.question
        section = question.section if question else None
        if section and int(section.flow_id) == int(flow_id):
            return True
    return False


def _candidate_segments(analysis: AIAnalysis, finding: dict) -> list[Segment]:
    if _integrated_scope_is_ambiguous(analysis):
        # Historical integrated analyses did not persist which flow fed the
        # prompt. When multiple flows now exist, rebinding by question-code text
        # would be ambiguous, so approval fails closed instead of guessing.
        return []

    query = (
        Segment.query
        .join(Interview, Segment.interview_id == Interview.id)
        .filter(
            Interview.project_id == analysis.project_id,
            Segment.speaker_role == "respondent",
        )
        .order_by(Segment.id.asc())
    )

    if analysis.interview_id is not None:
        query = query.filter(Segment.interview_id == analysis.interview_id)

    source_interview_ids = _stored_source_interview_ids(analysis)
    if source_interview_ids is not None:
        if not source_interview_ids:
            return []
        query = query.filter(Interview.id.in_(sorted(source_interview_ids)))

    candidates = query.all()
    source_flow_id = _analysis_source_flow_id(analysis)

    participant_codes = {
        str(code).strip()
        for code in (finding.get("participant_codes") or [])
        if str(code).strip()
    }
    question_codes = {
        str(code).strip()
        for code in (finding.get("question_codes") or [])
        if str(code).strip()
    }

    filtered = []
    for segment in candidates:
        if analysis.question_id is not None:
            if not _segment_has_question_id(segment, int(analysis.question_id)):
                continue
        elif source_flow_id is not None and not _segment_has_flow_mapping(segment, source_flow_id):
            continue

        if participant_codes:
            participant = segment.interview.participant if segment.interview else None
            code = participant.participant_code if participant else None
            if not code or str(code) not in participant_codes:
                continue

        # A persisted question_id is the canonical scope and supersedes model-
        # generated question-code strings. For broader analyses, codes remain a
        # useful qualifier but are evaluated only inside the canonical flow when
        # one is known.
        if question_codes and analysis.question_id is None:
            seg_codes = _segment_question_codes(segment, flow_id=source_flow_id)
            if not seg_codes.intersection(question_codes):
                continue

        filtered.append(segment)

    return filtered


def resolve_finding_source_segment_ids(analysis: AIAnalysis, finding: dict) -> list[int]:
    """Resolve an evidence quote to respondent Segment IDs within the analysis scope."""
    quote = _normalize_text(finding.get("evidence_quote"))
    if not quote:
        return []

    matches = []
    for segment in _candidate_segments(analysis, finding):
        source = _normalize_text(segment.text)
        if not source:
            continue

        if quote == source:
            matches.append(segment.id)
            continue

        # Short snippets are too ambiguous for substring matching.
        if len(quote) >= 8 and (quote in source or source in quote):
            matches.append(segment.id)

    return sorted(set(matches))


def prepare_analysis_for_approval(analysis: AIAnalysis) -> tuple[dict, list[dict]]:
    """
    Attach source_segment_ids to every finding.

    Returns (normalized_content, unresolved_findings). Approval must fail when any
    finding has no evidence quote or cannot be resolved to source Segment rows.
    """
    try:
        content = json.loads(analysis.content_json or "{}")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("AIAnalysis.content_json がJSONとして解釈できません") from exc

    if not isinstance(content, dict):
        raise ValueError("AIAnalysis.content_json はJSON objectである必要があります")

    findings = content.get("findings") or []
    if not isinstance(findings, list) or not findings:
        raise ValueError("承認には findings が1件以上必要です")

    ambiguous_integrated_scope = _integrated_scope_is_ambiguous(analysis)
    unresolved = []
    normalized_findings = []
    for index, raw_finding in enumerate(findings, start=1):
        if not isinstance(raw_finding, dict):
            unresolved.append({"finding_no": index, "reason": "finding is not an object"})
            normalized_findings.append(raw_finding)
            continue

        finding = dict(raw_finding)
        evidence_quote = str(finding.get("evidence_quote") or "").strip()
        source_ids = resolve_finding_source_segment_ids(analysis, finding) if evidence_quote else []
        finding["source_segment_ids"] = source_ids
        normalized_findings.append(finding)

        if ambiguous_integrated_scope:
            unresolved.append({
                "finding_no": index,
                "reason": "integrated analysis has no source_flow_id while multiple project flows exist",
            })
        elif not evidence_quote:
            unresolved.append({"finding_no": index, "reason": "evidence_quote is empty"})
        elif not source_ids:
            unresolved.append({
                "finding_no": index,
                "reason": "evidence_quote could not be resolved to source segments",
                "evidence_quote": evidence_quote,
            })

    normalized_content = dict(content)
    normalized_content["findings"] = normalized_findings
    return normalized_content, unresolved


def _begin_approval_write(analysis: AIAnalysis) -> AIAnalysis:
    """Serialize provenance/evidence validation with the approval commit."""
    analysis_id = int(analysis.id)
    project_id = int(analysis.project_id)
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("analysis approval requires a clean database session")

    db.session.rollback()
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))
        current = db.session.get(AIAnalysis, analysis_id)
    else:
        current = (
            AIAnalysis.query
            .filter_by(id=analysis_id)
            .with_for_update()
            .first()
        )

    if current is None or int(current.project_id) != project_id:
        db.session.rollback()
        raise ValueError("AIAnalysis が見つかりません")
    return current


def set_analysis_review_status(
    analysis: AIAnalysis,
    status: str,
    note: str | None = None,
) -> tuple[AIAnalysis, list[dict]]:
    status = str(status or "").strip().lower()
    if status not in ALLOWED_REVIEW_STATUSES:
        raise ValueError("review_status は draft / approved / rejected のいずれかです")

    unresolved = []
    if status == "approved":
        analysis = _begin_approval_write(analysis)
        try:
            require_current_analysis_source_provenance(analysis)
        except AnalysisSourceProvenanceError as exc:
            db.session.rollback()
            return analysis, [{
                "finding_no": None,
                "reason": "analysis source provenance is stale or unprovable",
                "detail": str(exc),
            }]

        content, unresolved = prepare_analysis_for_approval(analysis)
        if unresolved:
            db.session.rollback()
            return analysis, unresolved
        analysis.content_json = json.dumps(content, ensure_ascii=False)
        analysis.reviewed_at = datetime.now(timezone.utc)
    else:
        analysis.reviewed_at = datetime.now(timezone.utc) if status == "rejected" else None

    analysis.review_status = status
    analysis.review_note = (str(note).strip() if note is not None else None) or None
    db.session.add(analysis)
    db.session.commit()
    return analysis, unresolved
