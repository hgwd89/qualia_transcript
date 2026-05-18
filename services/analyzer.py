"""
AI 考察生成サービス。
evidence_quote（実発言引用）を必ず含む findings を生成する。
"""
import json
from models import db
from models.interview import Interview
from models.interview_flow import InterviewFlowQuestion
from models.segment import Segment, UtteranceMapping
from models.analysis import AIAnalysis
from services.ai_client import call_structured, MODEL
from services.analysis_trace import build_per_question_trace

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


# ── インタビュー単位の分析 ────────────────────────────────────

def analyze_per_question(interview_id: int, question_id: int) -> AIAnalysis:
    interview = Interview.query.get(interview_id)
    question  = InterviewFlowQuestion.query.get(question_id)

    mappings = (
        UtteranceMapping.query
        .filter_by(question_id=question_id)
        .join(Segment, UtteranceMapping.segment_id == Segment.id)
        .filter(Segment.interview_id == interview_id, Segment.speaker_role == "respondent")
        .all()
    )

    participant = interview.participant
    code        = participant.participant_code if participant else "P??"
    utterances  = "\n".join(f'- {code}:「{m.segment.text}」' for m in mappings)

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

    # メタ情報はモデル出力に依存せず、アプリ側で正規化する
    canonical_q_code = question.question_code or f"Q{question.id}"
    normalized_findings = []
    for finding in (result.get("findings") or []):
        if not isinstance(finding, dict):
            continue
        f = dict(finding)
        f["question_codes"] = [canonical_q_code]
        normalized_findings.append(f)

    trace = build_per_question_trace(
        db.session,
        interview_id=interview_id,
        question_id=question_id,
    )

    normalized_result = {
        "question_id": question.id,
        "question_code": canonical_q_code,
        "question_text": question.question_text,
        "findings": normalized_findings,
        "implications": result.get("implications", ""),
        "unresolved": result.get("unresolved", ""),
        "source_segment_ids": trace["source_segment_ids"],
        "source_segment_quotes": trace["source_segment_quotes"],
        "quote_ids": trace["quote_ids"],
    }

    analysis = AIAnalysis(
        project_id=interview.project_id,
        interview_id=interview_id,
        question_id=question_id,
        analysis_type="per_question",
        title=f"{question.question_code} 考察",
        summary_text=normalized_result.get("implications", ""),
        content_json=json.dumps(normalized_result, ensure_ascii=False),
        quote_ids=json.dumps(trace["quote_ids"], ensure_ascii=False),
        source_segment_ids=json.dumps(trace["source_segment_ids"], ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


def analyze_interview_summary(interview_id: int) -> AIAnalysis:
    """インタビュー全体の要約考察（参加者別）。"""
    interview   = Interview.query.get(interview_id)
    if not interview:
        raise ValueError("interview が見つかりません")

    participant = interview.participant
    if participant:
        code = participant.participant_code or "P??"
        participant_name = participant.display_name or code
    else:
        code = "P??"
        participant_name = "参加者未設定"

    segments  = (
        Segment.query
        .filter_by(interview_id=interview_id, speaker_role="respondent")
        .order_by(Segment.seq)
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

    analysis = AIAnalysis(
        project_id=interview.project_id,
        interview_id=interview_id,
        analysis_type="per_participant",
        title=f"{code} インタビュー総括",
        summary_text=result.get("implications", ""),
        content_json=json.dumps(result, ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    interview.status = "analyzed"
    db.session.commit()
    return analysis


# ── プロジェクト横断分析 ──────────────────────────────────────

def analyze_cross_participants(project_id: int, question_id: int) -> AIAnalysis:
    """
    複数参加者の同一質問に対する横断分析。
    全インタビューの該当質問への回答を収集して比較する。
    """
    from models.project import Project
    project  = Project.query.get(project_id)
    question = InterviewFlowQuestion.query.get(question_id)

    if not project or not question:
        raise ValueError("project または question が見つかりません")

    utterances_by_participant = []
    for interview in project.interviews:
        participant = interview.participant
        if not participant:
            continue
        code = participant.participant_code

        mappings = (
            UtteranceMapping.query
            .filter_by(question_id=question_id)
            .join(Segment, UtteranceMapping.segment_id == Segment.id)
            .filter(Segment.interview_id == interview.id, Segment.speaker_role == "respondent")
            .all()
        )
        if mappings:
            texts = "／".join(f'「{m.segment.text}」' for m in mappings)
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
        f"【質問】[{question.question_code}] {question.question_text}\n\n"
        f"【参加者別発言】\n{utterance_text}\n\n"
        "共通点・相違点・注目発言・マーケティング示唆を分析してください。"
    )

    result = call_structured(system, user, CROSS_SCHEMA, schema_name="cross_analysis_result")

    analysis = AIAnalysis(
        project_id=project_id,
        interview_id=None,
        question_id=question_id,
        analysis_type="cross_participant",
        title=f"横断分析: [{question.question_code}] {question.question_text[:40]}",
        summary_text=result.get("implications", ""),
        content_json=json.dumps(result, ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


def analyze_project_integrated(project_id: int) -> AIAnalysis:
    """
    プロジェクト全体の統合分析（全参加者・全セクション横断）。
    """
    from models.project import Project
    project = Project.query.get(project_id)
    if not project:
        raise ValueError("project が見つかりません")

    flows = project.interview_flows
    flow  = flows[0] if flows else None

    sections_data = []
    if flow:
        for section in flow.sections:
            questions_data = []
            for q in section.questions:
                utterances_by_p = []
                for interview in project.interviews:
                    participant = interview.participant
                    if not participant:
                        continue
                    code = participant.participant_code
                    mappings = (
                        UtteranceMapping.query
                        .filter_by(question_id=q.id)
                        .join(Segment, UtteranceMapping.segment_id == Segment.id)
                        .filter(Segment.interview_id == interview.id,
                                Segment.speaker_role == "respondent")
                        .all()
                    )
                    if mappings:
                        texts = "／".join(f'「{m.segment.text}」' for m in mappings)
                        utterances_by_p.append(f"{code}: {texts}")
                if utterances_by_p:
                    questions_data.append(
                        f"[{q.question_code}] {q.question_text}\n" + "\n".join(utterances_by_p)
                    )
            if questions_data:
                sections_data.append(f"【{section.title}】\n" + "\n\n".join(questions_data))

    if not sections_data:
        raise ValueError("分析対象の発言が見つかりません（先にマッピングを実行してください）")

    full_text   = "\n\n".join(sections_data)
    p_count     = sum(1 for iv in project.interviews if iv.participant)

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

    analysis = AIAnalysis(
        project_id=project_id,
        interview_id=None,
        question_id=None,
        analysis_type="integrated",
        title=f"{project.name} 統合分析",
        summary_text=result.get("implications", ""),
        content_json=json.dumps(result, ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis
