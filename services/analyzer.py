"""
AI 考察生成サービス。
evidence_quote（実発言引用）を必ず含む findings を生成する。
"""
from collections.abc import Callable
import json
from models import db
from models.interview import Interview
from models.interview_flow import InterviewFlowQuestion
from models.segment import Segment
from models.analysis import AIAnalysis
from services.ai_client import call_structured, MODEL
from services.analysis_source_provenance import (
    PROVENANCE_KEY,
    AnalysisSourceProvenanceError,
    _mapped_respondent_segments,
    capture_analysis_source_provenance,
    source_provenance_matches_scope,
)
from services.project_flow_scope import resolve_integrated_analysis_scope

ResultWriteGuard = Callable[[], object]

# ── 共通スキーマ定義 ──────────────────────────────────────────

FINDING_ITEM = {
    "type": "object",
    "properties": {
        "point":             {"type": "string"},
        "evidence_quote":    {"type": "string"},
        "participant_codes": {"type": "array", "items": {"type": "string"}},
        "question_codes":    {"type": "array", "items": {"type": "string"}},
        "confidence":        {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["point", "evidence_quote", "participant_codes", "question_codes", "confidence"],
    "additionalProperties": False,
}

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings":     {"type": "array", "items": FINDING_ITEM},
        "implications": {"type": "string"},
        "unresolved":   {"type": "string"},
    },
    "required": ["findings", "implications", "unresolved"],
    "additionalProperties": False,
}

CROSS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings":          {"type": "array", "items": FINDING_ITEM},
        "common_points":     {"type": "string"},
        "differences":       {"type": "string"},
        "notable_responses": {"type": "string"},
        "implications":      {"type": "string"},
        "unresolved":        {"type": "string"},
    },
    "required": ["findings", "common_points", "differences", "notable_responses", "implications", "unresolved"],
    "additionalProperties": False,
}

INTEGRATED_SCHEMA = {
    "type": "object",
    "properties": {
        "findings":               {"type": "array", "items": FINDING_ITEM},
        "common_themes":          {"type": "string"},
        "key_differences":        {"type": "string"},
        "representative_quotes":  {"type": "array", "items": {"type": "string"}},
        "implications":           {"type": "string"},
        "cautions":               {"type": "string"},
        "unresolved":             {"type": "string"},
    },
    "required": ["findings", "common_themes", "key_differences", "representative_quotes",
                 "implications", "cautions", "unresolved"],
    "additionalProperties": False,
}


def _question_flow_id(question: InterviewFlowQuestion) -> int | None:
    section = question.section if question else None
    return int(section.flow_id) if section and section.flow_id is not None else None


def _question_project_id(question: InterviewFlowQuestion) -> int | None:
    section = question.section if question else None
    flow = section.flow if section else None
    return int(flow.project_id) if flow and flow.project_id is not None else None


def _canonical_question_code(question: InterviewFlowQuestion) -> str:
    return str(question.question_code or f"Q{question.id}")


def _require_result_write_guard(result_write_guard: ResultWriteGuard | None) -> ResultWriteGuard:
    """Reject canonical analysis saves that are not owned by a durable attempt."""
    if result_write_guard is None:
        raise RuntimeError("analysis save requires a durable result-write guard")
    return result_write_guard


def _require_unchanged_source_provenance(
    expected: dict,
    analysis_type: str,
    project_id: int,
    *,
    interview_id: int | None = None,
    question_id: int | None = None,
) -> None:
    """Fail closed when provider inputs changed before the result commit."""
    ok, reason = source_provenance_matches_scope(
        expected,
        analysis_type,
        project_id,
        interview_id=interview_id,
        question_id=question_id,
    )
    if not ok:
        raise AnalysisSourceProvenanceError(reason)


# ── インタビュー単位の分析 ────────────────────────────────────

def analyze_per_question(
    interview_id: int,
    question_id: int,
    *,
    result_write_guard: ResultWriteGuard | None = None,
) -> AIAnalysis:
    interview = Interview.query.get(interview_id)
    question = InterviewFlowQuestion.query.get(question_id)
    if not interview or not question:
        raise ValueError("interview または question が見つかりません")

    question_flow_id = _question_flow_id(question)
    if interview.flow_id is None or question_flow_id != int(interview.flow_id):
        raise ValueError("question が interview の割当フローに属していません")

    result_write_guard = _require_result_write_guard(result_write_guard)
    source_provenance = capture_analysis_source_provenance(
        "per_question",
        int(interview.project_id),
        interview_id=int(interview_id),
        question_id=int(question_id),
    )

    segments = _mapped_respondent_segments(interview_id, question_id)
    participant = interview.participant
    code = (participant.participant_code or "P??") if participant else "P??"
    utterances = "\n".join(f'- {code}:「{segment.text}」' for segment in segments)

    system = (
        "あなたは定性調査の専門アナリストです。"
        "インタビューの発言から重要な発見を抽出してください。"
        "出力は必ず日本語で記述してください。"
        "evidence_quote は必ず実際の発言テキストをそのまま引用し、参加者コードを添えてください。"
        "発言にない内容を断定しないでください。推測は推測として明記してください。"
        "単一または少数の発言だけを根拠に、一般化した市場傾向や因果を断定しないでください。"
        "implications と unresolved も、提示された発言根拠から言える範囲に限定してください。"
    )
    user = (
        f"【質問】{question.question_text}\n\n"
        f"【発言】\n{utterances or '（発言なし）'}\n\n"
        "この質問に対する回答から発見事項、マーケティング示唆、積み残し課題を抽出してください。"
        "回答は日本語で返してください。"
    )

    result = call_structured(system, user, FINDINGS_SCHEMA, schema_name="analysis_result")

    canonical_q_code = _canonical_question_code(question)
    normalized_findings = []
    for finding in (result.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        f = dict(finding)
        f["question_codes"] = [canonical_q_code]
        normalized_findings.append(f)

    normalized_result = {
        "question_id": question.id,
        "question_code": canonical_q_code,
        "question_text": question.question_text,
        "findings": normalized_findings,
        "implications": result.get("implications", ""),
        "unresolved": result.get("unresolved", ""),
    }

    result_write_guard()
    _require_unchanged_source_provenance(
        source_provenance,
        "per_question",
        int(interview.project_id),
        interview_id=int(interview_id),
        question_id=int(question_id),
    )
    normalized_result[PROVENANCE_KEY] = source_provenance

    analysis = AIAnalysis(
        project_id=interview.project_id,
        interview_id=interview_id,
        question_id=question_id,
        analysis_type="per_question",
        title=f"{question.question_code} 考察",
        summary_text=normalized_result.get("implications", ""),
        content_json=json.dumps(normalized_result, ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


def analyze_interview_summary(
    interview_id: int,
    *,
    result_write_guard: ResultWriteGuard | None = None,
) -> AIAnalysis:
    """インタビュー全体の要約考察（参加者別）。"""
    interview = Interview.query.get(interview_id)
    if not interview:
        raise ValueError("interview が見つかりません")

    result_write_guard = _require_result_write_guard(result_write_guard)
    source_provenance = capture_analysis_source_provenance(
        "per_participant",
        int(interview.project_id),
        interview_id=int(interview_id),
    )

    participant = interview.participant
    if participant:
        code = participant.participant_code or "P??"
        participant_name = participant.display_name or code
    else:
        code = "P??"
        participant_name = "参加者未設定"

    segments = (
        Segment.query
        .filter_by(interview_id=interview_id, speaker_role="respondent")
        .order_by(Segment.seq.asc(), Segment.id.asc())
        .all()
    )
    full_text = "\n".join(f'- 「{s.text}」' for s in segments)

    system = (
        "あなたは定性調査の専門アナリストです。"
        "インタビュー全体を俯瞰した考察を提供してください。"
        "出力は必ず日本語で記述してください。"
        "evidence_quote は必ず実際の発言テキストを引用してください。"
        "発言にない内容を断定せず、推測は推測として明記してください。"
        "単一または少数の発言だけを根拠に、一般化した市場傾向や因果を断定しないでください。"
        "implications と unresolved も、提示された発言根拠から言える範囲に限定してください。"
    )
    user = (
        f"【参加者】{code} {participant_name}\n\n"
        f"【発言録】\n{full_text or '（発言なし）'}\n\n"
        "このインタビュー全体から重要な発見、マーケティング示唆、積み残し課題を抽出してください。"
        "回答は日本語で返してください。"
    )

    result = call_structured(system, user, FINDINGS_SCHEMA, schema_name="summary_result")

    result_write_guard()
    _require_unchanged_source_provenance(
        source_provenance,
        "per_participant",
        int(interview.project_id),
        interview_id=int(interview_id),
    )
    normalized_result = dict(result)
    normalized_result[PROVENANCE_KEY] = source_provenance

    analysis = AIAnalysis(
        project_id=interview.project_id,
        interview_id=interview_id,
        analysis_type="per_participant",
        title=f"{code} インタビュー総括",
        summary_text=normalized_result.get("implications", ""),
        content_json=json.dumps(normalized_result, ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    interview.status = "analyzed"
    db.session.commit()
    return analysis


# ── プロジェクト横断分析 ──────────────────────────────────────

def analyze_cross_participants(
    project_id: int,
    question_id: int,
    *,
    result_write_guard: ResultWriteGuard | None = None,
) -> AIAnalysis:
    """
    複数参加者の同一質問に対する横断分析。
    全インタビューの該当質問への回答を収集して比較する。
    """
    from models.project import Project
    project = Project.query.get(project_id)
    question = InterviewFlowQuestion.query.get(question_id)

    if not project or not question:
        raise ValueError("project または question が見つかりません")

    question_flow_id = _question_flow_id(question)
    if _question_project_id(question) != int(project_id) or question_flow_id is None:
        raise ValueError("question が project のインタビューフローに属していません")

    result_write_guard = _require_result_write_guard(result_write_guard)
    source_provenance = capture_analysis_source_provenance(
        "cross_participant",
        int(project_id),
        question_id=int(question_id),
    )

    utterances_by_participant = []
    for interview in sorted(project.interviews, key=lambda row: int(row.id)):
        if interview.flow_id is None or int(interview.flow_id) != question_flow_id:
            continue
        participant = interview.participant
        if not participant:
            continue
        code = str(participant.participant_code or "")

        segments = _mapped_respondent_segments(interview.id, question_id)
        if segments:
            texts = "／".join(f'「{segment.text}」' for segment in segments)
            utterances_by_participant.append(f"{code}: {texts}")

    if not utterances_by_participant:
        raise ValueError("分析対象の発言が見つかりません（先にマッピングを実行してください）")

    utterance_text = "\n".join(utterances_by_participant)

    system = (
        "あなたは定性調査の専門アナリストです。"
        "複数の参加者の同一質問に対する回答を横断分析してください。"
        "evidence_quote は必ず実際の発言テキストをそのまま引用し、参加者コードを添えてください。"
        "発言にない内容を断定せず、推測は推測として明記してください。"
    )
    user = (
        f"【質問】[{_canonical_question_code(question)}] {question.question_text}\n\n"
        f"【参加者別発言】\n{utterance_text}\n\n"
        "共通点・相違点・注目発言・マーケティング示唆を分析してください。"
    )

    result = call_structured(system, user, CROSS_SCHEMA, schema_name="cross_analysis_result")
    canonical_q_code = _canonical_question_code(question)
    normalized_findings = []
    for finding in (result.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        f = dict(finding)
        f["question_codes"] = [canonical_q_code]
        normalized_findings.append(f)
    normalized_result = dict(result)
    normalized_result["question_id"] = int(question.id)
    normalized_result["question_code"] = canonical_q_code
    normalized_result["findings"] = normalized_findings

    result_write_guard()
    _require_unchanged_source_provenance(
        source_provenance,
        "cross_participant",
        int(project_id),
        question_id=int(question_id),
    )
    normalized_result[PROVENANCE_KEY] = source_provenance

    analysis = AIAnalysis(
        project_id=project_id,
        interview_id=None,
        question_id=question_id,
        analysis_type="cross_participant",
        title=f"横断分析: [{question.question_code}] {question.question_text[:40]}",
        summary_text=normalized_result.get("implications", ""),
        content_json=json.dumps(normalized_result, ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


def analyze_project_integrated(
    project_id: int,
    *,
    result_write_guard: ResultWriteGuard | None = None,
) -> AIAnalysis:
    """
    プロジェクト全体の統合分析（全参加者・全セクション横断）。
    """
    from models.project import Project
    project = Project.query.get(project_id)
    if not project:
        raise ValueError("project が見つかりません")

    scope = resolve_integrated_analysis_scope(project)
    result_write_guard = _require_result_write_guard(result_write_guard)
    source_provenance = capture_analysis_source_provenance(
        "integrated",
        int(project_id),
    )

    flow = scope.flow
    source_interview_ids = set(scope.interview_ids)

    sections_data = []
    source_question_ids: list[int] = []
    allowed_question_codes: set[str] = set()
    for section in sorted(flow.sections, key=lambda row: (int(row.seq), int(row.id))):
        questions_data = []
        for q in sorted(section.questions, key=lambda row: (int(row.seq), int(row.id))):
            source_question_ids.append(int(q.id))
            allowed_question_codes.add(_canonical_question_code(q))
            utterances_by_p = []
            for interview in sorted(project.interviews, key=lambda row: int(row.id)):
                if int(interview.id) not in source_interview_ids:
                    continue
                participant = interview.participant
                if not participant:
                    continue
                code = str(participant.participant_code or "")
                segments = _mapped_respondent_segments(interview.id, q.id)
                if segments:
                    texts = "／".join(f'「{segment.text}」' for segment in segments)
                    utterances_by_p.append(f"{code}: {texts}")
            if utterances_by_p:
                questions_data.append(
                    f"[{_canonical_question_code(q)}] {q.question_text}\n" + "\n".join(utterances_by_p)
                )
        if questions_data:
            sections_data.append(f"【{section.title}】\n" + "\n\n".join(questions_data))

    if not sections_data:
        raise ValueError("分析対象の発言が見つかりません（先にマッピングを実行してください）")

    full_text = "\n\n".join(sections_data)
    p_count = len(scope.interview_ids)

    system = (
        "あなたは定性調査の専門アナリストです。"
        "プロジェクト全体のインタビューを横断的に分析してください。"
        "evidence_quote は必ず実際の発言テキストを引用し参加者コードを添えてください。"
        "発言にない内容を断定せず、推測は推測として明記してください。"
    )
    user = (
        f"【調査プロジェクト】{project.name}\n"
        f"【クライアント】{project.client or '未設定'}\n"
        f"【調査目的】{project.research_objective or '未設定'}\n"
        f"【参加者数】{p_count}名\n\n"
        f"【発言データ】\n{full_text}\n\n"
        "主要な発見事項、共通テーマ、参加者間の違い、代表発言、マーケティング示唆、注意点を分析してください。"
    )

    result = call_structured(system, user, INTEGRATED_SCHEMA, schema_name="integrated_result")
    normalized_findings = []
    for finding in (result.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        f = dict(finding)
        raw_codes = [
            str(code).strip()
            for code in (finding.get("question_codes") or [])
            if str(code).strip()
        ]
        f["question_codes"] = [code for code in raw_codes if code in allowed_question_codes]
        normalized_findings.append(f)
    normalized_result = dict(result)
    normalized_result["source_flow_id"] = int(flow.id)
    normalized_result["source_question_ids"] = sorted(set(source_question_ids))
    normalized_result["source_interview_ids"] = sorted(source_interview_ids)
    normalized_result["findings"] = normalized_findings

    result_write_guard()
    _require_unchanged_source_provenance(
        source_provenance,
        "integrated",
        int(project_id),
    )
    normalized_result[PROVENANCE_KEY] = source_provenance

    analysis = AIAnalysis(
        project_id=project_id,
        interview_id=None,
        question_id=None,
        analysis_type="integrated",
        title=f"{project.name} 統合分析",
        summary_text=normalized_result.get("implications", ""),
        content_json=json.dumps(normalized_result, ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis
