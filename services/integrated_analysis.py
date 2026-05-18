"""
Integrated interview analysis (no-ai dry-run baseline).

Important:
- Segment.text must remain unchanged.
- raw transcripts must remain unchanged.
- This module does not call external APIs.
- This module does not write DB rows (save is intentionally unsupported).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from models.analysis import AIAnalysis
from models.interview import Interview
from models.participant import Participant
from models.segment import Segment
from models.segment_flag import SegmentFlag
from models.speaker_assignment import SpeakerAssignment


def _parse_json(text: str | None) -> dict[str, Any]:
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _latest_semantic_analysis(interview_id: int) -> AIAnalysis | None:
    return (
        AIAnalysis.query
        .filter_by(interview_id=interview_id, analysis_type="semantic_clusters")
        .order_by(AIAnalysis.id.desc())
        .first()
    )


def _latest_per_question_analyses(interview_id: int) -> list[AIAnalysis]:
    rows = (
        AIAnalysis.query
        .filter_by(interview_id=interview_id, analysis_type="per_question")
        .order_by(AIAnalysis.id.desc())
        .all()
    )
    latest_by_qid: dict[int, AIAnalysis] = {}
    no_qid_rows: list[AIAnalysis] = []
    for row in rows:
        if row.question_id is None:
            no_qid_rows.append(row)
            continue
        if row.question_id not in latest_by_qid:
            latest_by_qid[row.question_id] = row
    # keep deterministic order by question_id asc, then no_qid rows by id desc
    selected = [latest_by_qid[k] for k in sorted(latest_by_qid.keys())]
    selected.extend(no_qid_rows)
    return selected


def _collect_flag_map(interview_id: int) -> dict[int, set[str]]:
    rows = (
        SegmentFlag.query
        .join(Segment, Segment.id == SegmentFlag.segment_id)
        .filter(Segment.interview_id == interview_id)
        .all()
    )
    out: dict[int, set[str]] = {}
    for row in rows:
        out.setdefault(row.segment_id, set()).add(row.flag_type)
    return out


def _collect_speaker_assignment_map(interview_id: int) -> dict[str, SpeakerAssignment]:
    rows = SpeakerAssignment.query.filter_by(interview_id=interview_id).all()
    return {r.speaker_label: r for r in rows if r.speaker_label}


def _effective_role(seg: Segment, assignment_map: dict[str, SpeakerAssignment]) -> str:
    assignment = assignment_map.get(seg.speaker_label or "")
    if assignment and assignment.speaker_role:
        return assignment.speaker_role
    return seg.speaker_role or "unknown"


def _resolve_participant_info(
    seg: Segment,
    interview: Interview,
    assignment_map: dict[str, SpeakerAssignment],
    participant_map: dict[int, Participant],
) -> tuple[int | None, str | None, str | None]:
    assignment = assignment_map.get(seg.speaker_label or "")
    participant_id = None
    if assignment and assignment.participant_id:
        participant_id = assignment.participant_id
    elif seg.participant_id:
        participant_id = seg.participant_id
    elif interview.participant_id:
        participant_id = interview.participant_id

    participant = participant_map.get(participant_id) if participant_id else None
    if not participant:
        return participant_id, None, None
    return participant_id, participant.participant_code, participant.display_name


def _supporting_quote_item(
    seg: Segment,
    flags: set[str],
    role: str,
    participant_code: str | None,
) -> dict[str, Any]:
    return {
        "quote_id": f"Q{seg.id}_1",
        "segment_id": seg.id,
        "text": seg.text,  # exact Segment.text
        "speaker_label": seg.speaker_label,
        "speaker_role": role,
        "participant_code": participant_code,
        "flags": sorted(list(flags)),
    }


def run_integrated_interview_analysis(
    interview_id: int,
    no_ai: bool = True,
    save: bool = False,
    max_quotes: int = 20,
    include_needs_review: bool = False,
) -> dict[str, Any]:
    if not no_ai:
        raise NotImplementedError("AI-integrated mode is not implemented yet. Use --no-ai.")
    if save:
        raise NotImplementedError("Save mode is not implemented for integrated analysis yet.")

    interview = Interview.query.get(interview_id)
    if not interview:
        raise ValueError(f"interview_id={interview_id} not found")

    participant_rows = Participant.query.filter_by(project_id=interview.project_id).all()
    participant_map = {p.id: p for p in participant_rows}

    assignment_map = _collect_speaker_assignment_map(interview_id)
    flag_map = _collect_flag_map(interview_id)

    segments = (
        Segment.query
        .filter_by(interview_id=interview_id)
        .order_by(Segment.seq.asc())
        .all()
    )
    segment_by_id = {s.id: s for s in segments}

    excluded_segment_count = 0
    excluded_by_reason = {
        "exclude_flag": 0,
        "needs_review_flag": 0,
        "non_respondent_role": 0,
    }

    cautions: list[str] = []
    candidate_segments: list[Segment] = []
    candidate_flags: dict[int, set[str]] = {}
    candidate_role: dict[int, str] = {}

    for seg in segments:
        flags = flag_map.get(seg.id, set())
        role = _effective_role(seg, assignment_map)

        if role in ("moderator", "observer"):
            excluded_segment_count += 1
            excluded_by_reason["non_respondent_role"] += 1
            continue
        if "exclude" in flags:
            excluded_segment_count += 1
            excluded_by_reason["exclude_flag"] += 1
            cautions.append(f"segment-{seg.id} is excluded by flag")
            continue
        if "needs_review" in flags and not include_needs_review:
            excluded_segment_count += 1
            excluded_by_reason["needs_review_flag"] += 1
            cautions.append(f"segment-{seg.id} is marked needs_review and excluded from evidence")
            continue

        candidate_segments.append(seg)
        candidate_flags[seg.id] = set(flags)
        candidate_role[seg.id] = role
        if "needs_review" in flags:
            cautions.append(f"segment-{seg.id} is marked needs_review")

    # speaker assignment summary
    labels_in_interview = sorted({(s.speaker_label or "") for s in segments if s.speaker_label})
    unresolved_labels = sorted([lbl for lbl in labels_in_interview if lbl and lbl not in assignment_map])
    speaker_assignment_summary = {
        "mapped_label_count": len(assignment_map),
        "unresolved_labels": unresolved_labels,
    }

    # semantic clusters as primary evidence source
    semantic_analysis = _latest_semantic_analysis(interview_id)
    semantic_payload = _parse_json(semantic_analysis.content_json if semantic_analysis else None)
    cluster_summaries = semantic_payload.get("cluster_summaries") if isinstance(semantic_payload, dict) else []
    if not isinstance(cluster_summaries, list):
        cluster_summaries = []

    semantic_cluster_insights: list[dict[str, Any]] = []
    semantic_source_ids_in_order: list[int] = []

    for cs in cluster_summaries:
        if not isinstance(cs, dict):
            continue
        raw_ids = cs.get("evidence_source_segment_ids") or []
        ids: list[int] = []
        for x in raw_ids:
            try:
                sid = int(x)
            except Exception:
                continue
            if sid in segment_by_id and sid in candidate_flags:
                ids.append(sid)
                semantic_source_ids_in_order.append(sid)

        src_quotes = [segment_by_id[sid].text for sid in ids]
        if cs.get("caution"):
            cautions.append(str(cs.get("caution")))
        semantic_cluster_insights.append({
            "cluster_id": cs.get("cluster_id"),
            "theme": cs.get("theme", ""),
            "summary": cs.get("summary", ""),
            "evidence_source_segment_ids": ids,
            "source_segment_quotes": src_quotes,
        })

    # per-question analyses with trace are evidence; missing trace remains supplementary
    per_question_rows = _latest_per_question_analyses(interview_id)
    question_insights: list[dict[str, Any]] = []
    unresolved_questions: list[str] = []
    question_findings_without_traceability = 0
    per_question_source_ids_in_order: list[int] = []

    for row in per_question_rows:
        content = _parse_json(row.content_json)
        question_id = content.get("question_id", row.question_id)
        question_code = content.get("question_code", "")
        question_text = content.get("question_text", "")
        implications = content.get("implications", "")
        unresolved = content.get("unresolved", "")
        findings = content.get("findings") or []

        raw_source_ids = content.get("source_segment_ids")
        if raw_source_ids is None and row.source_segment_ids:
            try:
                parsed_source_ids = json.loads(row.source_segment_ids)
            except Exception:
                parsed_source_ids = []
            raw_source_ids = parsed_source_ids if isinstance(parsed_source_ids, list) else []
        source_ids: list[int] = []
        if isinstance(raw_source_ids, list):
            for x in raw_source_ids:
                try:
                    sid = int(x)
                except Exception:
                    continue
                if sid in segment_by_id and sid in candidate_flags:
                    source_ids.append(sid)
                    per_question_source_ids_in_order.append(sid)

        traceability = "traceable_source_segment_ids" if source_ids else "supplementary_no_source_segment_ids"
        if not source_ids and isinstance(findings, list):
            question_findings_without_traceability += len(findings)
        if unresolved:
            unresolved_questions.append(f"{question_code or question_id}: {unresolved}")
        question_insights.append({
            "question_id": question_id,
            "question_code": question_code,
            "question_text": question_text,
            "summary": implications,
            "evidence_source_segment_ids": source_ids,
            "traceability": traceability,
        })

    if question_findings_without_traceability > 0:
        cautions.append(
            f"per_question findings ({question_findings_without_traceability}) lack source_segment_ids; treated as supplementary only"
        )

    # supporting quotes (quote flags first, then traceable per-question evidence, then semantic evidence, then respondent fallback)
    quote_items: list[dict[str, Any]] = []
    selected_segment_ids: set[int] = set()

    quote_first = [s for s in candidate_segments if "quote" in candidate_flags.get(s.id, set())]
    quote_first.sort(key=lambda s: s.seq)

    for seg in quote_first:
        if len(quote_items) >= max_quotes:
            break
        pid, pcode, _ = _resolve_participant_info(seg, interview, assignment_map, participant_map)
        _ = pid  # pid resolved for participant_insights, not needed here
        quote_items.append(_supporting_quote_item(seg, candidate_flags.get(seg.id, set()), candidate_role[seg.id], pcode))
        selected_segment_ids.add(seg.id)

    for sid in per_question_source_ids_in_order:
        if len(quote_items) >= max_quotes:
            break
        if sid in selected_segment_ids:
            continue
        seg = segment_by_id.get(sid)
        if not seg:
            continue
        pid, pcode, _ = _resolve_participant_info(seg, interview, assignment_map, participant_map)
        _ = pid
        quote_items.append(_supporting_quote_item(seg, candidate_flags.get(seg.id, set()), candidate_role[sid], pcode))
        selected_segment_ids.add(sid)

    for sid in semantic_source_ids_in_order:
        if len(quote_items) >= max_quotes:
            break
        if sid in selected_segment_ids:
            continue
        seg = segment_by_id.get(sid)
        if not seg:
            continue
        pid, pcode, _ = _resolve_participant_info(seg, interview, assignment_map, participant_map)
        _ = pid
        quote_items.append(_supporting_quote_item(seg, candidate_flags.get(seg.id, set()), candidate_role[sid], pcode))
        selected_segment_ids.add(sid)

    for seg in candidate_segments:
        if len(quote_items) >= max_quotes:
            break
        if seg.id in selected_segment_ids:
            continue
        pid, pcode, _ = _resolve_participant_info(seg, interview, assignment_map, participant_map)
        _ = pid
        quote_items.append(_supporting_quote_item(seg, candidate_flags.get(seg.id, set()), candidate_role[seg.id], pcode))
        selected_segment_ids.add(seg.id)

    # participant insights (deterministic, no AI)
    participant_bucket: dict[int, dict[str, Any]] = {}
    for seg in candidate_segments:
        pid, pcode, pname = _resolve_participant_info(seg, interview, assignment_map, participant_map)
        if not pid:
            continue
        bucket = participant_bucket.setdefault(pid, {
            "participant_id": pid,
            "participant_code": pcode,
            "display_name": pname,
            "attributes": {},
            "segment_count": 0,
            "quote_segment_count": 0,
            "speaker_labels": set(),
            "insights": [],
        })
        bucket["segment_count"] += 1
        if "quote" in candidate_flags.get(seg.id, set()):
            bucket["quote_segment_count"] += 1
        if seg.speaker_label:
            bucket["speaker_labels"].add(seg.speaker_label)

    participant_insights: list[dict[str, Any]] = []
    for pid, bucket in sorted(participant_bucket.items(), key=lambda x: x[0]):
        p = participant_map.get(pid)
        attrs = {}
        if p:
            attrs = {a.attribute_key: a.attribute_value for a in p.attributes}
        bucket["attributes"] = attrs
        bucket["speaker_labels"] = sorted(list(bucket["speaker_labels"]))
        bucket["insights"] = [
            f"対象発話数: {bucket['segment_count']}",
            f"quoteフラグ発話数: {bucket['quote_segment_count']}",
        ]
        participant_insights.append(bucket)

    # key findings (deterministic placeholders from existing analyses)
    key_findings: list[dict[str, Any]] = []
    for idx, c in enumerate(semantic_cluster_insights, start=1):
        key_findings.append({
            "finding_id": f"SC{idx}",
            "summary": c.get("summary", ""),
            "confidence": "medium",
            "question_codes": [],
            "cluster_ids": [c.get("cluster_id")],
            "evidence_source_segment_ids": c.get("evidence_source_segment_ids", []),
        })

    for idx, q in enumerate(question_insights, start=1):
        if q.get("summary"):
            key_findings.append({
                "finding_id": f"Q{idx}",
                "summary": q.get("summary", ""),
                "confidence": "low",
                "question_codes": [q.get("question_code")] if q.get("question_code") else [],
                "cluster_ids": [],
                "evidence_source_segment_ids": q.get("evidence_source_segment_ids", []),
            })

    # traceable source quotes
    source_segment_ids = sorted(list({
        q["segment_id"] for q in quote_items if q.get("segment_id") is not None
    }))
    source_segment_quotes = [segment_by_id[sid].text for sid in source_segment_ids if sid in segment_by_id]

    source_quote_exact_match_count = 0
    for sid, qtext in zip(source_segment_ids, source_segment_quotes):
        seg = segment_by_id.get(sid)
        if seg and seg.text == qtext:
            source_quote_exact_match_count += 1

    evidence_map = []
    quote_ids_by_segment = {
        q["segment_id"]: q["quote_id"]
        for q in quote_items
        if q.get("segment_id") is not None
    }
    for f in key_findings:
        eids = [int(x) for x in (f.get("evidence_source_segment_ids") or []) if isinstance(x, int) or str(x).isdigit()]
        eids = [int(x) for x in eids if int(x) in quote_ids_by_segment]
        evidence_map.append({
            "finding_id": f.get("finding_id"),
            "source_segment_ids": eids,
            "quote_ids": [quote_ids_by_segment[sid] for sid in eids],
        })

    flag_summary = {
        "quote_count": sum(1 for f in candidate_flags.values() if "quote" in f),
        "favorite_count": sum(1 for f in candidate_flags.values() if "favorite" in f),
        "exclude_count": excluded_by_reason["exclude_flag"],
        "needs_review_count": excluded_by_reason["needs_review_flag"] + sum(
            1 for f in candidate_flags.values() if "needs_review" in f
        ),
    }

    payload = {
        "analysis_type": "integrated_interview_analysis",
        "interview_id": interview_id,
        "mode": "no_ai_dry_run",
        "key_findings": key_findings,
        "supporting_quotes": quote_items,
        "participant_insights": participant_insights,
        "question_insights": question_insights,
        "semantic_cluster_insights": semantic_cluster_insights,
        "product_or_brand_mentions": [],
        "implications": [],
        "unresolved_questions": unresolved_questions,
        "cautions": sorted(list(dict.fromkeys(cautions))),
        "evidence_map": evidence_map,
        "source_segment_ids": source_segment_ids,
        "source_segment_quotes": source_segment_quotes,
        "flag_summary": flag_summary,
        "speaker_assignment_summary": speaker_assignment_summary,
        "models": {
            "integrator_model": "none",
            "source_models": {
                "semantic_clusters": semantic_analysis.model_used if semantic_analysis else "none",
                "per_question": "existing_saved_analyses",
            },
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {
        "ok": True,
        "payload": payload,
        "supporting_quote_count": len(quote_items),
        "semantic_cluster_insight_count": len(semantic_cluster_insights),
        "question_insight_count": len(question_insights),
        "participant_insight_count": len(participant_insights),
        "excluded_segment_count": excluded_segment_count,
        "unresolved_speaker_labels": unresolved_labels,
        "source_quote_exact_match_count": source_quote_exact_match_count,
        "api_call_count": 0,
        "db_update_performed": False,
    }
