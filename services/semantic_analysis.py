"""
切片化 + embeddingクラスタリング + AI要約。

重要:
- Segment.text は変更しない
- raw transcript は変更しない
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

import numpy as np
import openai

import config
from models import db
from models.analysis import AIAnalysis
from models.interview import Interview
from models.setting import AppSetting
from services.ai_client import MODEL as CHAT_MODEL, call_structured
from services.fragmentation import (
    build_analysis_fragments,
    collect_candidate_segments,
)

EMBEDDING_MODEL = "text-embedding-3-small"
_QUESTION_END_RE = re.compile(r"[？?]\s*$")
_MODERATOR_LIKE_PHRASES = (
    "ですか",
    "ますか",
    "教えてください",
    "確認",
    "わかりました",
    "なるほど",
    "はいはい",
    "この季節でも",
    "どうするんでしたっけ",
    "取ってきてもいいですか",
)


def _client() -> openai.OpenAI:
    api_key = AppSetting.get("openai_api_key") or config.OPENAI_API_KEY
    return openai.OpenAI(api_key=api_key)


def embed_fragments(fragments: list[dict[str, Any]]) -> np.ndarray:
    texts = [(f.get("text") or "").strip() for f in fragments]
    texts = [t for t in texts if t]
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)

    client = _client()
    res = client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
    vectors = [d.embedding for d in res.data]
    return np.array(vectors, dtype=np.float32)


def _choose_cluster_count(n: int) -> int:
    if n < 10:
        return 1
    if n <= 30:
        # 3〜5
        return max(3, min(5, round(np.sqrt(n))))
    # 5〜8
    return max(5, min(8, round(np.sqrt(n))))


def cluster_embeddings(embeddings: np.ndarray) -> list[int]:
    if embeddings.size == 0:
        return []
    n = embeddings.shape[0]
    if n <= 1:
        return [0] * n

    try:
        from sklearn.cluster import AgglomerativeClustering
    except Exception as e:
        raise RuntimeError("sklearn is not available. Please install scikit-learn.") from e

    k = _choose_cluster_count(n)
    k = max(1, min(k, n))
    if k == 1:
        return [0] * n

    model = AgglomerativeClustering(
        n_clusters=k,
        metric="cosine",
        linkage="average",
    )
    labels = model.fit_predict(embeddings)
    return [int(x) for x in labels.tolist()]


def _is_question_like(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _QUESTION_END_RE.search(t):
        return True
    return ("ですか" in t) or ("ますか" in t)


def _is_probe_like(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return any(p in t for p in _MODERATOR_LIKE_PHRASES)


def _is_evidence_worthy(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 12:
        return False
    if _is_question_like(t):
        return False
    if _is_probe_like(t):
        return False
    return True


def _evaluate_cluster_eligibility(
    cluster_fragments: list[dict[str, Any]],
    representative_fragments: list[dict[str, Any]],
) -> tuple[bool, str | None, dict[str, int]]:
    total = len(cluster_fragments)
    question_like = sum(1 for f in cluster_fragments if _is_question_like((f.get("text") or "")))
    probe_like = sum(1 for f in cluster_fragments if _is_probe_like((f.get("text") or "")))
    evidence_worthy = sum(1 for f in cluster_fragments if _is_evidence_worthy((f.get("text") or "")))
    rep_count = len(representative_fragments)

    stats = {
        "total": total,
        "question_like": question_like,
        "probe_like": probe_like,
        "evidence_worthy": evidence_worthy,
        "representative_count": rep_count,
    }

    if total <= 1 and evidence_worthy <= 1:
        return False, "single_fragment_weak", stats
    if total > 0 and question_like / total >= 0.5:
        return False, "question_like_dominant", stats
    if total > 0 and probe_like / total >= 0.5:
        return False, "probe_dominant", stats
    if evidence_worthy == 0:
        return False, "no_evidence_respondent", stats
    return True, None, stats


def select_representative_fragments(
    fragments: list[dict[str, Any]],
    embeddings: np.ndarray,
    labels: list[int],
    per_cluster: int = 4,
) -> dict[int, list[dict[str, Any]]]:
    if not fragments or embeddings.size == 0 or not labels:
        return {}

    idx_by_cluster: dict[int, list[int]] = defaultdict(list)
    for i, label in enumerate(labels):
        idx_by_cluster[label].append(i)

    reps: dict[int, list[dict[str, Any]]] = {}
    for cid, idxs in idx_by_cluster.items():
        vecs = embeddings[idxs]
        centroid = np.mean(vecs, axis=0, keepdims=True)
        num = np.dot(vecs, centroid.T).reshape(-1)
        den = (np.linalg.norm(vecs, axis=1) * np.linalg.norm(centroid, axis=1)[0] + 1e-8)
        sims = num / den

        ranked = sorted(zip(idxs, sims.tolist()), key=lambda x: x[1], reverse=True)
        selected: list[dict[str, Any]] = []
        for idx, score in ranked[:max(3, per_cluster)]:
            f = fragments[idx]
            selected.append({
                "text": f.get("text"),
                "source_segment_ids": f.get("source_segment_ids", []),
                "start_sec": f.get("start_sec"),
                "end_sec": f.get("end_sec"),
                "speaker_label": f.get("speaker_label"),
                "speaker_role": f.get("speaker_role"),
                "participant_id": f.get("participant_id"),
                "representative_score": round(float(score), 4),
            })
        reps[cid] = selected
    return reps


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_summary": {"type": "string"},
        "cluster_summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cluster_id": {"type": "integer"},
                    "theme": {"type": "string"},
                    "summary": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "evidence_source_segment_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                    "source_segment_quotes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "evidence_quote_mode": {
                        "type": "string",
                        "enum": [
                            "exact_segment_quote",
                            "normalized_fragment_quote",
                            "ai_generated_summary_quote",
                        ],
                    },
                    "implication": {"type": "string"},
                    "caution": {"type": "string"},
                },
                "required": [
                    "cluster_id",
                    "theme",
                    "summary",
                    "evidence_quote",
                    "evidence_source_segment_ids",
                    "source_segment_quotes",
                    "evidence_quote_mode",
                    "implication",
                    "caution",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overall_summary", "cluster_summaries"],
    "additionalProperties": False,
}


def summarize_clusters_with_ai(clusters: list[dict[str, Any]]) -> dict[str, Any]:
    system = (
        "あなたは定性調査の分析者です。"
        "出力は日本語。根拠にない推測や過度な一般化を禁止します。"
        "因果関係を断定せず、『寄与している』『影響している』等の断定的因果表現は避けてください。"
        "根拠発話が少ない場合は示唆を限定的に書き、caution に不確実性を明示してください。"
        "代表発話にない行動・動機・効果を推測しないでください。"
        "evidence_quote は必ず与えられた source_segment_quotes から完全一致で1つ選んでください。"
        "引用を言い換えたり新規作成してはいけません。"
        "evidence_source_segment_ids には引用元IDを入れてください。"
        "evidence_quote_mode は exact_segment_quote または normalized_fragment_quote を選んでください。"
        "ASR誤認識の可能性があれば caution に書いてください。"
        "市場全体や他者一般への波及（認知度向上・使用率向上など）を推測しないでください。"
        "implication は『この参加者において示唆されること』に限定してください。"
    )
    user = (
        "以下は respondent 発話のクラスタ代表切片です。"
        "クラスタごとにテーマ要約・示唆を作成してください。\n\n"
        f"{json.dumps(clusters, ensure_ascii=False)}"
    )
    return call_structured(system, user, _SUMMARY_SCHEMA, schema_name="semantic_cluster_summary")


def run_semantic_cluster_analysis(
    interview_id: int,
    save: bool = False,
    max_segments: int | None = None,
    no_ai: bool = False,
) -> dict[str, Any]:
    interview = db.session.get(Interview, interview_id)
    if not interview:
        raise ValueError(f"interview_id={interview_id} not found")

    candidates = collect_candidate_segments(interview_id)
    if max_segments:
        candidates = candidates[:max_segments]

    fragments, frag_stats = build_analysis_fragments(candidates)
    if not fragments:
        return {
            "ok": False,
            "reason": "no_fragments_after_filter",
            "interview_id": interview_id,
            "candidate_segment_count": frag_stats.get("candidate_count", 0),
            "fragment_count": 0,
            "excluded_count": frag_stats.get("excluded_count", 0),
            "excluded_counts": frag_stats.get("excluded_counts", {}),
            "embedding_api_call_count": 0,
            "summary_api_call_count": 0,
        }

    embeddings = embed_fragments(fragments)
    embedding_api_call_count = 1
    labels = cluster_embeddings(embeddings)
    cluster_counter = Counter(labels)
    reps = select_representative_fragments(fragments, embeddings, labels, per_cluster=4)
    # トレース補強: source_segment_id -> 元Segment.text
    source_id_to_quote: dict[int, str] = {}
    for fr in fragments:
        for sid, quote in zip(fr.get("source_segment_ids") or [], fr.get("source_segment_quotes") or []):
            if sid is None:
                continue
            source_id_to_quote[int(sid)] = quote

    clusters: list[dict[str, Any]] = []
    eligible_clusters: list[dict[str, Any]] = []
    excluded_cluster_reasons = Counter()
    for cid in sorted(cluster_counter.keys()):
        idxs = [i for i, lab in enumerate(labels) if lab == cid]
        cluster_fragments = [fragments[i] for i in idxs]
        rep_frags = reps.get(cid, [])
        eligible, ineligible_reason, eligibility_stats = _evaluate_cluster_eligibility(cluster_fragments, rep_frags)

        cluster_item = {
            "cluster_id": int(cid),
            "count": int(cluster_counter[cid]),
            "representative_fragments": rep_frags,
            "representative_source_segment_quotes": list(dict.fromkeys([
                source_id_to_quote.get(int(sid), "")
                for rf in rep_frags
                for sid in (rf.get("source_segment_ids") or [])
                if sid is not None and source_id_to_quote.get(int(sid), "")
            ])),
            "eligible_for_summary": eligible,
            "ineligible_reason": ineligible_reason,
            "eligibility_stats": eligibility_stats,
        }
        clusters.append(cluster_item)
        if eligible:
            eligible_clusters.append(cluster_item)
        elif ineligible_reason:
            excluded_cluster_reasons[ineligible_reason] += 1

    cluster_source_quotes_map: dict[int, list[tuple[int, str]]] = {}
    for c in clusters:
        pairs: list[tuple[int, str]] = []
        for rf in c.get("representative_fragments") or []:
            for sid in (rf.get("source_segment_ids") or []):
                if sid is None:
                    continue
                sid_i = int(sid)
                q = source_id_to_quote.get(sid_i, "")
                if q:
                    pairs.append((sid_i, q))
        # dedupe keep order
        seen = set()
        deduped = []
        for sid, q in pairs:
            key = (sid, q)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((sid, q))
        cluster_source_quotes_map[int(c["cluster_id"])] = deduped

    summary_api_call_count = 0
    cluster_summaries = None
    overall_summary = None
    evidence_quotes: list[str] = []
    if not no_ai:
        if eligible_clusters:
            ai_out = summarize_clusters_with_ai(eligible_clusters)
            summary_api_call_count = 1
            overall_summary = ai_out.get("overall_summary")
            cluster_summaries = ai_out.get("cluster_summaries")
            if isinstance(cluster_summaries, list):
                normalized_summaries = []
                for item in cluster_summaries:
                    if not isinstance(item, dict):
                        continue
                    cid = int(item.get("cluster_id", -1))
                    candidates = cluster_source_quotes_map.get(cid, [])
                    ids = [int(x) for x in (item.get("evidence_source_segment_ids") or []) if str(x).isdigit()]
                    ev = (item.get("evidence_quote") or "").strip()

                    if not ids and ev:
                        # evidence_quote が候補引用に含まれる/部分一致する場合はそのsidを採用
                        matched = [(sid, q) for sid, q in candidates if (ev == q or ev in q or q in ev)]
                        if matched:
                            ids = [matched[0][0]]
                    if not ids and candidates:
                        ids = [candidates[0][0]]

                    src_quotes = [
                        source_id_to_quote.get(sid, "")
                        for sid in ids
                        if source_id_to_quote.get(sid, "")
                    ]
                    mode = item.get("evidence_quote_mode") or "ai_generated_summary_quote"
                    ev = item.get("evidence_quote") or ""
                    # 元Segment完全一致引用を優先。なければ mode を補正。
                    if src_quotes:
                        if ev not in src_quotes:
                            ev = src_quotes[0]
                            mode = "exact_segment_quote"
                        elif mode == "ai_generated_summary_quote":
                            mode = "exact_segment_quote"
                    else:
                        mode = "ai_generated_summary_quote"

                    item["evidence_quote"] = ev
                    item["evidence_source_segment_ids"] = ids
                    item["source_segment_quotes"] = src_quotes
                    item["evidence_quote_mode"] = mode
                    normalized_summaries.append(item)
                cluster_summaries = normalized_summaries
                evidence_quotes = [x.get("evidence_quote", "") for x in cluster_summaries if isinstance(x, dict)]
        else:
            overall_summary = "要約対象クラスタがありません。"
            cluster_summaries = []

    result_payload = {
        "interview_id": interview_id,
        "candidate_segment_count": frag_stats.get("candidate_count", len(candidates)),
        "fragment_count": len(fragments),
        "excluded_count": frag_stats.get("excluded_count", 0),
        "excluded_counts": frag_stats.get("excluded_counts", {}),
        "fragments": fragments,
        "clusters": clusters,
        "eligible_cluster_count": len(eligible_clusters),
        "excluded_cluster_count": len(clusters) - len(eligible_clusters),
        "excluded_cluster_reasons": dict(excluded_cluster_reasons),
        "representative_quotes": reps,
        "cluster_summaries": cluster_summaries,
        "overall_summary": overall_summary,
        "source_segment_ids": sorted({sid for f in fragments for sid in (f.get("source_segment_ids") or [])}),
        "source_segment_quotes": list(dict.fromkeys([
            q
            for f in fragments
            for q in (f.get("source_segment_quotes") or [])
            if q
        ])),
        "evidence_quotes": evidence_quotes,
        "models": {
            "embedding_model": EMBEDDING_MODEL,
            "summary_model": None if no_ai else CHAT_MODEL,
        },
        "created_at": datetime.now(timezone.utc).isoformat(),
        "note": "逐語本文は変更していません",
    }

    saved_analysis_id = None
    if save:
        analysis = AIAnalysis(
            project_id=interview.project_id,
            interview_id=interview.id,
            question_id=None,
            analysis_type="semantic_clusters",
            title="Semantic Cluster Analysis",
            summary_text=overall_summary or f"{len(clusters)}クラスタを抽出しました",
            content_json=json.dumps(result_payload, ensure_ascii=False),
            model_used=f"{EMBEDDING_MODEL}+{CHAT_MODEL if not no_ai else 'no-ai-summary'}",
            created_at=datetime.now(timezone.utc),
        )
        db.session.add(analysis)
        db.session.commit()
        saved_analysis_id = analysis.id

    return {
        "ok": True,
        "interview_id": interview_id,
        "candidate_segment_count": result_payload["candidate_segment_count"],
        "fragment_count": result_payload["fragment_count"],
        "excluded_count": result_payload["excluded_count"],
        "excluded_counts": result_payload["excluded_counts"],
        "cluster_count": len(clusters),
        "cluster_examples": clusters[:3],
        "embedding_api_call_count": embedding_api_call_count,
        "summary_api_call_count": summary_api_call_count,
        "save_performed": bool(save),
        "saved_analysis_id": saved_analysis_id,
        "payload": result_payload,
    }
