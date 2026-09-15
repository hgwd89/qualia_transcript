"""
発言セグメント → 質問項目への AI 自動マッピング。
"""
from collections.abc import Callable

from models import db
from models.segment import UtteranceMapping, UtteranceMappingProvenance
from services.ai_client import call_structured
from services.mapping_source_provenance import (
    MappingSourceProvenanceError,
    build_mapping_source_manifest,
    capture_mapping_source_provenance,
    mapping_source_provenance_status,
    serialize_mapping_source_provenance,
)

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
    """Validate provider IDs/completeness before any canonical replacement."""
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
    """Map all respondent segments and persist one source-fenced AI generation."""
    if result_write_guard is None:
        raise RuntimeError("mapping save requires a durable result-write guard")

    # Build one immutable source view. Both the provider prompt and the persisted
    # fingerprint derive from this same manifest so the claimed source cannot
    # diverge from the bytes logically supplied to the provider.
    source_manifest = build_mapping_source_manifest(int(interview_id))
    segment_rows = list(source_manifest["segments"])
    question_rows = list(source_manifest["questions"])
    if not segment_rows or not question_rows:
        return 0

    source_provenance = capture_mapping_source_provenance(int(interview_id))
    # capture_mapping_source_provenance re-queries by design for public callers.
    # The provider generation must bind specifically to the manifest above, so
    # reject any drift that occurred between those two source reads before the
    # external call begins.
    current, reason = mapping_source_provenance_status(
        source_provenance,
        interview_id=int(interview_id),
    )
    if not current:
        raise MappingSourceProvenanceError(reason)

    q_list = "\n".join(
        f'- id:{row["id"]} [{row["question_code"]}] {row["question_text"]}'
        for row in question_rows
    )
    seg_list = "\n".join(
        f'- segment_id:{row["id"]} 「{row["text"]}」'
        for row in segment_rows
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
        allowed_segment_ids={int(row["id"]) for row in segment_rows},
        allowed_question_ids={int(row["id"]) for row in question_rows},
    )

    # External work is complete. Verify durable lease first, then re-read the
    # canonical source. A stale worker or changed source cannot delete/replace the
    # prior mapping generation.
    result_write_guard()
    current, reason = mapping_source_provenance_status(
        source_provenance,
        interview_id=int(interview_id),
    )
    if not current:
        raise MappingSourceProvenanceError(reason)

    seg_ids = [int(row["id"]) for row in segment_rows]
    existing_mappings = (
        UtteranceMapping.query
        .filter(UtteranceMapping.segment_id.in_(seg_ids))
        .all()
    )
    for existing_mapping in existing_mappings:
        db.session.delete(existing_mapping)
    db.session.flush()

    serialized_provenance = serialize_mapping_source_provenance(source_provenance)
    for mapping in normalized_mappings:
        row = UtteranceMapping(
            segment_id=mapping["segment_id"],
            question_id=mapping.get("question_id"),
            mapped_by="ai",
            confidence=mapping.get("confidence", 0.0),
            is_unclassified=mapping.get("is_unclassified", False),
        )
        db.session.add(row)
        db.session.flush()
        db.session.add(UtteranceMappingProvenance(
            mapping_id=int(row.id),
            source_provenance_json=serialized_provenance,
        ))

    from models.interview import Interview
    interview = db.session.get(Interview, int(interview_id))
    if interview is None:
        db.session.rollback()
        raise MappingSourceProvenanceError("interview disappeared before mapping commit")
    interview.status = "mapped"
    db.session.commit()
    return len(normalized_mappings)
