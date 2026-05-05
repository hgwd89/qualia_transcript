"""
Respondent発話のセマンティッククラスタ分析（CLI基盤）。

注意:
- 逐語本文（Segment.text）は変更しない
- raw transcript は変更しない
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import openai

import config
from models import db
from models.analysis import AIAnalysis
from models.interview import Interview
from models.segment import Segment
from models.setting import AppSetting
from services.ai_client import MODEL as CHAT_MODEL, call_structured

EMBEDDING_MODEL = "text-embedding-3-small"

_BACKCHANNEL_PATTERNS = [
    r"^はい$",
    r"^ええ$",
    r"^うん$",
    r"^なるほど$",
    r"^わかりました$",
    r"^そうですね$",
    r"^そうですか$",
    r"^ありがとうございます$",
]
_BACKCHANNEL_RE = re.compile("|".join(_BACKCHANNEL_PATTERNS))
_MODERATOR_LIKE_RE = re.compile(r"\?|？|ですか|ますか|でしょうか|教えて|確認|聞こえ|大丈夫")


@dataclass
class SegmentRow:
    segment_id: int
    seq: int
    speaker_label: str | None
    speaker_role: str | None
    start_sec: float | None
    end_sec: float | None
    text: str


def _client() -> openai.OpenAI:
    api_key = AppSetting.get("openai_api_key") or config.OPENAI_API_KEY
    return openai.OpenAI(api_key=api_key)


def collect_respondent_segments(interview_id: int) -> list[SegmentRow]:
    rows = (
        Segment.query
        .filter_by(interview_id=interview_id, speaker_role="respondent")
        .order_by(Segment.seq.asc())
        .all()
    )
    out: list[SegmentRow] = []
    for s in rows:
        out.append(SegmentRow(
            segment_id=s.id,
            seq=s.seq,
            speaker_label=s.speaker_label,
            speaker_role=s.speaker_role,
            start_sec=s.start_sec,
            end_sec=s.end_sec,
            text=s.text or "",
        ))
    return out


def filter_segments_for_semantic_analysis(
    segments: list[SegmentRow],
    min_chars: int = 8,
) -> tuple[list[SegmentRow], dict[str, int]]:
    kept: list[SegmentRow] = []
    excluded = Counter()

    for s in segments:
        text = (s.text or "").strip()
        if not text:
            excluded["empty"] += 1
            continue
        if len(text) < min_chars:
            excluded["too_short"] += 1
            continue
        if _BACKCHANNEL_RE.search(text):
            excluded["backchannel"] += 1
            continue
        if _MODERATOR_LIKE_RE.search(text):
            excluded["moderator_like"] += 1
            continue
        kept.append(s)
    return kept, dict(excluded)


def embed_texts(texts: list[str], model: str = EMBEDDING_MODEL) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    client = _client()
    res = client.embeddings.create(model=model, input=texts)
    vectors = [d.embedding for d in res.data]
    return np.array(vectors, dtype=np.float32)


def _estimate_cluster_count(n: int) -> int:
    if n <= 1:
        return 1
    if n <= 6:
        return 2
    if n <= 12:
        return 3
    if n <= 25:
        return 4
    if n <= 40:
        return 5
    return 6


def cluster_embeddings(embeddings: np.ndarray, n_clusters: int | None = None) -> list[int]:
    if embeddings.size == 0:
        return []
    n = embeddings.shape[0]
    if n == 1:
        return [0]

    try:
        from sklearn.cluster import AgglomerativeClustering
    except Exception as e:
        raise RuntimeError("sklearn is not available. Please install scikit-learn.") from e

    k = n_clusters or _estimate_cluster_count(n)
    k = max(2, min(k, n))

    model = AgglomerativeClustering(
        n_clusters=k,
        metric="cosine",
        linkage="average",
    )
    labels = model.fit_predict(embeddings)
    return [int(x) for x in labels.tolist()]


def select_representative_quotes(
    segments: list[SegmentRow],
    embeddings: np.ndarray,
    labels: list[int],
    per_cluster: int = 3,
) -> dict[int, list[dict[str, Any]]]:
    if not segments or embeddings.size == 0 or not labels:
        return {}

    cluster_to_idx: dict[int, list[int]] = defaultdict(list)
    for idx, label in enumerate(labels):
        cluster_to_idx[label].append(idx)

    reps: dict[int, list[dict[str, Any]]] = {}
    for cluster_id, idxs in cluster_to_idx.items():
        vecs = embeddings[idxs]
        centroid = np.mean(vecs, axis=0, keepdims=True)

        # cosine similarity
        num = np.dot(vecs, centroid.T).reshape(-1)
        den = (np.linalg.norm(vecs, axis=1) * np.linalg.norm(centroid, axis=1)[0] + 1e-8)
        sims = num / den

        ranked = sorted(
            zip(idxs, sims.tolist()),
            key=lambda x: x[1],
            reverse=True,
        )
        chosen: list[dict[str, Any]] = []
        for i, score in ranked[:max(1, per_cluster)]:
            s = segments[i]
            chosen.append({
                "segment_id": s.segment_id,
                "seq": s.seq,
                "speaker_label": s.speaker_label,
                "start_sec": s.start_sec,
                "end_sec": s.end_sec,
                "text": s.text,
                "representative_score": round(float(score), 4),
            })
        reps[cluster_id] = chosen
    return reps


_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_summary": {"type": "string"},
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cluster_id": {"type": "integer"},
                    "theme": {"type": "string"},
                    "summary": {"type": "string"},
                    "evidence_quotes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "implication": {"type": "string"},
                },
                "required": ["cluster_id", "theme", "summary", "evidence_quotes", "implication"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["overall_summary", "clusters"],
    "additionalProperties": False,
}


def summarize_clusters_with_ai(
    interview_id: int,
    cluster_payload: list[dict[str, Any]],
) -> dict[str, Any]:
    system = (
        "あなたは定性調査の分析者です。"
        "出力は日本語のみ。過度な一般化や断定を避け、引用可能な発話に基づいて要約してください。"
    )
    user = (
        f"interview_id={interview_id}\n"
        "以下は respondent 発話のクラスタ情報です。各クラスタのテーマ要約と示唆を作成してください。\n\n"
        f"{json.dumps(cluster_payload, ensure_ascii=False)}"
    )
    return call_structured(
        system=system,
        user=user,
        json_schema=_SUMMARY_SCHEMA,
        schema_name="semantic_cluster_summary",
    )


def run_semantic_cluster_analysis(
    interview_id: int,
    save: bool = False,
    max_segments: int | None = None,
    no_ai: bool = False,
) -> dict[str, Any]:
    interview = db.session.get(Interview, interview_id)
    if not interview:
        raise ValueError(f"interview_id={interview_id} not found")

    source_segments = collect_respondent_segments(interview_id)
    filtered_segments, excluded_counts = filter_segments_for_semantic_analysis(source_segments)
    if max_segments:
        filtered_segments = filtered_segments[:max_segments]

    texts = [s.text for s in filtered_segments]
    if not texts:
        return {
            "ok": False,
            "reason": "no_segments_after_filter",
            "interview_id": interview_id,
            "source_segment_count": len(source_segments),
            "filtered_segment_count": 0,
            "excluded_counts": excluded_counts,
            "openai_api_call_count": 0,
        }

    openai_api_call_count = 0
    embeddings = embed_texts(texts, model=EMBEDDING_MODEL)
    openai_api_call_count += 1

    labels = cluster_embeddings(embeddings)
    cluster_counts = Counter(labels)
    reps = select_representative_quotes(filtered_segments, embeddings, labels, per_cluster=3)

    clusters: list[dict[str, Any]] = []
    for cid in sorted(cluster_counts.keys()):
        clusters.append({
            "cluster_id": int(cid),
            "count": int(cluster_counts[cid]),
            "representative_quotes": reps.get(cid, []),
        })

    ai_summary = None
    if not no_ai:
        ai_summary = summarize_clusters_with_ai(interview_id, clusters)
        openai_api_call_count += 1

    payload = {
        "interview_id": interview_id,
        "source_segment_count": len(source_segments),
        "filtered_segment_count": len(filtered_segments),
        "excluded_counts": excluded_counts,
        "cluster_count": len(cluster_counts),
        "clusters": clusters,
        "ai_summary": ai_summary,
        "note": "逐語本文は変更していません",
    }

    saved_analysis_id = None
    if save:
        summary_text = (
            ai_summary.get("overall_summary", "")
            if isinstance(ai_summary, dict) else
            f"{len(cluster_counts)}クラスタを抽出しました"
        )
        analysis = AIAnalysis(
            project_id=interview.project_id,
            interview_id=interview.id,
            question_id=None,
            analysis_type="semantic_clusters",
            title="Semantic Cluster Analysis",
            summary_text=summary_text,
            content_json=json.dumps(payload, ensure_ascii=False),
            model_used=f"{EMBEDDING_MODEL}+{CHAT_MODEL if not no_ai else 'no-ai-summary'}",
            created_at=datetime.now(timezone.utc),
        )
        db.session.add(analysis)
        db.session.commit()
        saved_analysis_id = analysis.id

    payload["ok"] = True
    payload["saved_analysis_id"] = saved_analysis_id
    payload["openai_api_call_count"] = openai_api_call_count
    return payload
