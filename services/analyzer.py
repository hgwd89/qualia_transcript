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
from services.claude_client import call_structured, call_text_streaming, MODEL

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point":             {"type": "string"},
                    "evidence_quote":    {"type": "string"},
                    "participant_codes": {"type": "array", "items": {"type": "string"}},
                    "question_codes":    {"type": "array", "items": {"type": "string"}},
                    "confidence":        {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["point", "evidence_quote", "participant_codes",
                             "question_codes", "confidence"],
                "additionalProperties": False,
            },
        },
        "implications": {"type": "string"},
        "unresolved":   {"type": "string"},
    },
    "required": ["findings", "implications", "unresolved"],
    "additionalProperties": False,
}


def analyze_per_question(interview_id: int, question_id: int) -> AIAnalysis:
    interview = Interview.query.get(interview_id)
    question  = InterviewFlowQuestion.query.get(question_id)

    # この質問に紐づく発言を取得
    mappings = (
        UtteranceMapping.query
        .filter_by(question_id=question_id)
        .join(Segment, UtteranceMapping.segment_id == Segment.id)
        .filter(Segment.interview_id == interview_id, Segment.speaker_role == "respondent")
        .all()
    )

    participant = interview.participant
    code = participant.participant_code if participant else "P??"
    utterances = "\n".join(f'- {code}:「{m.segment.text}」' for m in mappings)

    system = (
        "あなたは定性調査の専門アナリストです。"
        "インタビューの発言から重要な発見を抽出してください。"
        "evidence_quote は必ず実際の発言テキストをそのまま引用し、参加者コードを添えてください。"
    )
    user = (
        f"【質問】{question.question_text}\n\n"
        f"【発言】\n{utterances or '（発言なし）'}\n\n"
        "この質問に対する回答から発見事項、マーケティング示唆、積み残し課題を抽出してください。"
    )

    result = call_structured(system, user, FINDINGS_SCHEMA, schema_name="analysis_result")

    analysis = AIAnalysis(
        project_id=interview.project_id,
        interview_id=interview_id,
        question_id=question_id,
        analysis_type="per_question",
        title=f"{question.question_code} 考察",
        summary_text=result.get("implications", ""),
        content_json=json.dumps(result, ensure_ascii=False),
        model_used=MODEL,
    )
    db.session.add(analysis)
    db.session.commit()
    return analysis


def analyze_interview_summary(interview_id: int) -> AIAnalysis:
    """インタビュー全体の要約考察"""
    interview  = Interview.query.get(interview_id)
    participant = interview.participant
    code = participant.participant_code if participant else "P??"

    segments = (
        Segment.query
        .filter_by(interview_id=interview_id, speaker_role="respondent")
        .order_by(Segment.seq)
        .all()
    )
    full_text = "\n".join(f'- 「{s.text}」' for s in segments)

    system = (
        "あなたは定性調査の専門アナリストです。"
        "インタビュー全体を俯瞰した考察を提供してください。"
        "evidence_quote は必ず実際の発言テキストを引用してください。"
    )
    user = (
        f"【参加者】{code} {participant.display_name or ''}\n\n"
        f"【発言録】\n{full_text or '（発言なし）'}\n\n"
        "このインタビュー全体から重要な発見、マーケティング示唆、積み残し課題を抽出してください。"
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
