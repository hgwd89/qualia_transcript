"""
発言セグメント → 質問項目への AI 自動マッピング。
"""
import json
from models import db
from models.interview import Interview
from models.interview_flow import InterviewFlowQuestion, InterviewFlowSection, InterviewFlow
from models.segment import Segment, UtteranceMapping
from services.ai_client import call_structured

MIN_CONFIDENCE_CLASSIFIED = 0.65

SCHEMA = {
    "type": "object",
    "properties": {
        "mappings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment_id":       {"type": "integer"},
                    "question_id":      {"type": ["integer", "null"]},
                    "confidence":       {"type": "number"},
                    "is_unclassified":  {"type": "boolean"},
                },
                "required": ["segment_id", "question_id", "confidence", "is_unclassified"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["mappings"],
    "additionalProperties": False,
}


def run_mapping(interview_id: int) -> int:
    """
    interview に紐づく全発言を質問項目にマッピングして DB 保存。
    戻り値: マッピング件数
    """
    interview = Interview.query.get(interview_id)
    if not interview or not interview.flow_id:
        return 0

    # 発言（respondent のみ対象）
    segments = (
        Segment.query
        .filter_by(interview_id=interview_id, speaker_role="respondent")
        .order_by(Segment.seq)
        .all()
    )
    if not segments:
        return 0

    # フロー全質問を取得
    flow     = interview.flow
    questions = []
    for section in flow.sections:
        for q in section.questions:
            questions.append(q)

    if not questions:
        return 0

    # プロンプト構築
    q_list = "\n".join(
        f'- id:{q.id} [{q.question_code}] {q.question_text}' for q in questions
    )
    seg_list = "\n".join(
        f'- segment_id:{s.id} 「{s.text}」' for s in segments
    )

    system = (
        "あなたは定性調査の専門家です。"
        "発言セグメントを、質問に直接答えている場合にのみ質問項目へ割り当ててください。"
        "関連が弱い・文脈不足・推測が必要な場合は is_unclassified=true, question_id=null を選んでください。"
        "複数候補がある場合は最も具体的に一致する質問を1つだけ選んでください。"
        "「睡眠・休暇の確保」と「休日行動・趣味・一人行動」を混同しないでください。"
        "調査者発話、確認発話、音声確認（聞こえ方確認）は分類しないでください。"
        "confidence は 0.0〜1.0 で表してください。"
    )
    user = (
        f"【質問項目一覧】\n{q_list}\n\n"
        f"【発言セグメント一覧】\n{seg_list}\n\n"
        "各発言を最も適切な質問項目 id に割り当ててください。"
        "回答が質問に直接答えていない場合は必ず unclassified にしてください。"
    )

    result = call_structured(system, user, SCHEMA, schema_name="utterance_mapping_result")
    mappings = result.get("mappings", [])

    # 既存マッピングを削除して再挿入
    seg_ids = [s.id for s in segments]
    UtteranceMapping.query.filter(UtteranceMapping.segment_id.in_(seg_ids)).delete(
        synchronize_session=False
    )

    normalized_mappings = []
    for m in mappings:
        segment_id = m["segment_id"]
        question_id = m.get("question_id")
        confidence = float(m.get("confidence", 0.0) or 0.0)
        is_unclassified = bool(m.get("is_unclassified", False))

        # 弱い分類は unclassified 側へ寄せる
        if question_id is None:
            is_unclassified = True
            confidence = min(confidence, 0.49)
        elif confidence < MIN_CONFIDENCE_CLASSIFIED:
            question_id = None
            is_unclassified = True

        normalized_mappings.append({
            "segment_id": segment_id,
            "question_id": question_id,
            "confidence": confidence,
            "is_unclassified": is_unclassified,
        })

    for m in normalized_mappings:
        db.session.add(UtteranceMapping(
            segment_id=m["segment_id"],
            question_id=m.get("question_id"),
            mapped_by="ai",
            confidence=m.get("confidence", 0.0),
            is_unclassified=m.get("is_unclassified", False),
        ))

    interview.status = "mapped"
    db.session.commit()
    return len(normalized_mappings)
