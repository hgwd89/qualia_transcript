"""
切片化 + embeddingクラスタリング + AI要約。

重要:
- Segment.text は変更しない
- raw transcript は変更しない
"""
from __future__ import annotations

import json
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
                    "implication": {"type": "string"},
                    "caution": {"type": "string"},
                },
                "required": ["cluster_id", "theme", "summary", "evidence_quote", "implication", "caution"],
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
        "各クラスタで evidence_quote を1つ必ず示し、ASR誤認識の可能性があれば caution に書いてください。"
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

    clusters: list[dict[str, Any]] = []
    for cid in sorted(cluster_counter.keys()):
        clusters.append({
            "cluster_id": int(cid),
            "count": int(cluster_counter[cid]),
            "representative_fragments": reps.get(cid, []),
        })

    summary_api_call_count = 0
    cluster_summaries = None
    overall_summary = None
    evidence_quotes: list[str] = []
    if not no_ai:
        ai_out = summarize_clusters_with_ai(clusters)
        summary_api_call_count = 1
        overall_summary = ai_out.get("overall_summary")
        cluster_summaries = ai_out.get("cluster_summaries")
        if isinstance(cluster_summaries, list):
            evidence_quotes = [x.get("evidence_quote", "") for x in cluster_summaries if isinstance(x, dict)]

    result_payload = {
        "interview_id": interview_id,
        "candidate_segment_count": frag_stats.get("candidate_count", len(candidates)),
        "fragment_count": len(fragments),
        "excluded_count": frag_stats.get("excluded_count", 0),
        "excluded_counts": frag_stats.get("excluded_counts", {}),
        "fragments": fragments,
        "clusters": clusters,
        "representative_quotes": reps,
        "cluster_summaries": cluster_summaries,
        "overall_summary": overall_summary,
        "source_segment_ids": sorted({sid for f in fragments for sid in (f.get("source_segment_ids") or [])}),
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
