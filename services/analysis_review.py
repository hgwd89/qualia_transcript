"""Human review and source-traceability helpers for AIAnalysis."""
import json
import re
from datetime import datetime, timezone

from models import db
from models.analysis import AIAnalysis
from models.interview import Interview
from models.segment import Segment


ALLOWED_REVIEW_STATUSES = {"draft", "approved", "rejected"}


def _normalize_text(value: str | None) -> str:
    text = str(value or "").strip()
    if "「" in text and "」" in text and text.find("「") < text.rfind("」"):
        text = text[text.find("「") + 1:text.rfind("」")]
    text = text.strip(' \t\r\n"\'“”‘’「」『』')
    return re.sub(r"\s+", " ", text).strip()


def _segment_question_codes(segment: Segment) -> set[str]:
    codes = set()
    for mapping in segment.utterance_mappings:
        question = mapping.question
        if question and question.question_code:
            codes.add(str(question.question_code))
    return codes


def _candidate_segments(analysis: AIAnalysis, finding: dict) -> list[Segment]:
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

    candidates = query.all()

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
        if participant_codes:
            participant = segment.interview.participant if segment.interview else None
            code = participant.participant_code if participant else None
            if not code or str(code) not in participant_codes:
                continue

        if question_codes:
            seg_codes = _segment_question_codes(segment)
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

        if not evidence_quote:
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
        content, unresolved = prepare_analysis_for_approval(analysis)
        if unresolved:
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
