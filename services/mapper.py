"""
発言セグメント → 質問項目への AI 自動マッピング。
"""
from collections.abc import Callable
from models import db
from models.interview import Interview
from models.segment import Segment, UtteranceMapping
from services.ai_client import call_structured

MIN_CONFIDENCE_CLASSIFIED = 0.65
ResultWriteGuard = Callable[[], object]


class MappingResultScopeError(ValueError):
    """Provider mapping output escaped or incompletely covered the requested scope."""


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


def _normalize_provider_mappings(
    mappings: list[dict],
    *,
    allowed_segment_ids: set[int],
    allowed_question_ids: set[int],
) -> list[dict]:
    """Validate provider IDs/completeness before any canonical replacement.

    Foreign but otherwise valid primary keys must never be accepted merely because
    database foreign-key constraints can resolve them. A mapping result is valid
    only when it contains exactly one row for every respondent segment in the
    requested interview and every non-null question belongs to that interview's
    selected flow.
    """
    normalized_mappings: list[dict] = []
    seen_segment_ids: set[int] = set()

    for mapping in mappings:
        segment_id = int(mapping["segment_id"])
        if segment_id not in allowed_segment_ids:
            raise MappingResultScopeError(
                f"provider returned segment_id outside mapping scope: {segment_id}"
            )
        if segment_id in seen_segment_ids:
            raise MappingResultScopeError(
                f"provider returned duplicate segment_id: {segment_id}"
            )
        seen_segment_ids.add(segment_id)

        raw_question_id = mapping.get("question_id")
        question_id = int(raw_question_id) if raw_question_id is not None else None
        if question_id is not None and question_id not in allowed_question_ids:
            raise MappingResultScopeError(
                f"provider returned question_id outside interview flow: {question_id}"
            )

        confidence = float(mapping.get("confidence", 0.0) or 0.0)
        is_unclassified = bool(mapping.get("is_unclassified", False))

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

    missing_segment_ids = allowed_segment_ids - seen_segment_ids
    if missing_segment_ids:
        rendered = ", ".join(str(value) for value in sorted(missing_segment_ids))
        raise MappingResultScopeError(
            f"provider mapping result omitted respondent segment_id(s): {rendered}"
        )

    return normalized_mappings


def run_mapping(
    interview_id: int,
    *,
    result_write_guard: ResultWriteGuard | None = None,
) -> int:
    """
    interview に紐づく全発言を質問項目にマッピングして DB 保存。
    戻り値: マッピング件数

    Canonical AI mappings may only be saved by a durable worker. The guard is
    required before provider work starts, then invoked again immediately before
    the canonical replacement commit to verify the current attempt lease.
    """
    interview = Interview.query.get(interview_id)
    if not interview or not interview.flow_id:
        return 0

    # 発言（respondent のみ対象）。Historical duplicate seq values are possible,
    # so ID is the deterministic tie-breaker for the provider input order.
    segments = (
        Segment.query
        .filter_by(interview_id=interview_id, speaker_role="respondent")
        .order_by(Segment.seq.asc(), Segment.id.asc())
        .all()
    )
    if not segments:
        return 0

    # フロー全質問を取得。Relationship order_by only contains seq, so add stable
    # ID tie-breakers here to prevent historical duplicate seq values from changing
    # the provider prompt order between runs.
    flow = interview.flow
    questions = []
    for section in sorted(flow.sections, key=lambda value: (value.seq, value.id)):
        questions.extend(sorted(section.questions, key=lambda value: (value.seq, value.id)))

    if not questions:
        return 0

    if result_write_guard is None:
        raise RuntimeError("mapping save requires a durable result-write guard")

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
    normalized_mappings = _normalize_provider_mappings(
        mappings,
        allowed_segment_ids={int(segment.id) for segment in segments},
        allowed_question_ids={int(question.id) for question in questions},
    )

    # External work is complete. Verify the durable attempt lease before any
    # existing canonical mapping is deleted or replaced.
    result_write_guard()

    # 既存マッピングを ORM 経由で削除し、DELETE を先に flush する。
    # SQLite は削除直後の ROWID を再利用し得るため、bulk delete のまま新規
    # mapping を追加すると同一 PK の stale object が identity map に残る。
    seg_ids = [s.id for s in segments]
    existing_mappings = (
        UtteranceMapping.query
        .filter(UtteranceMapping.segment_id.in_(seg_ids))
        .all()
    )
    for existing_mapping in existing_mappings:
        db.session.delete(existing_mapping)
    db.session.flush()

    for mapping in normalized_mappings:
        db.session.add(UtteranceMapping(
            segment_id=mapping["segment_id"],
            question_id=mapping.get("question_id"),
            mapped_by="ai",
            confidence=mapping.get("confidence", 0.0),
            is_unclassified=mapping.get("is_unclassified", False),
        ))

    interview.status = "mapped"
    db.session.commit()
    return len(normalized_mappings)
