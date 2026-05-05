"""
分析用切片（fragment）を Segment から非破壊で作る。

重要:
- Segment.text を変更しない
- raw transcript を変更しない
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from models.segment import Segment

_BACKCHANNEL_RE = re.compile(r"^(はい|ええ|うん|なるほど|わかりました|ありがとうございます|そうですね|そうですか)[。！!？?、,\s]*$")
_CONFIRM_RE = re.compile(r"聞こえ|確認|大丈夫|ですか|ますか|\?|？")
_SPLIT_RE = re.compile(r"(?<=[。！？\?])\s+|[\r\n]+")


def collect_candidate_segments(interview_id: int) -> list[dict[str, Any]]:
    rows = (
        Segment.query
        .filter_by(interview_id=interview_id, speaker_role="respondent")
        .order_by(Segment.seq.asc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for s in rows:
        out.append({
            "text": (s.text or "").strip(),
            "source_segment_ids": [s.id],
            "start_sec": s.start_sec,
            "end_sec": s.end_sec,
            "speaker_label": s.speaker_label,
            "speaker_role": s.speaker_role,
            "participant_id": s.participant_id,
            "is_noise": False,
            "seq": s.seq,
        })
    return out


def filter_noise_fragments(
    fragments: list[dict[str, Any]],
    min_chars: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    kept: list[dict[str, Any]] = []
    excluded = Counter()

    for fr in fragments:
        text = (fr.get("text") or "").strip()
        if not text:
            excluded["empty"] += 1
            continue
        if len(text) < min_chars:
            excluded["too_short"] += 1
            continue
        if _BACKCHANNEL_RE.search(text):
            excluded["backchannel"] += 1
            continue
        if _CONFIRM_RE.search(text) and len(text) < 32:
            excluded["confirm_or_probe"] += 1
            continue
        item = dict(fr)
        item["is_noise"] = False
        kept.append(item)
    return kept, dict(excluded)


def merge_short_fragments(
    fragments: list[dict[str, Any]],
    short_len: int = 20,
) -> list[dict[str, Any]]:
    if not fragments:
        return []
    out: list[dict[str, Any]] = []

    for fr in fragments:
        if not out:
            out.append(dict(fr))
            continue

        prev = out[-1]
        prev_text = (prev.get("text") or "").strip()
        cur_text = (fr.get("text") or "").strip()
        same_speaker = (
            prev.get("speaker_label") == fr.get("speaker_label")
            and prev.get("speaker_role") == fr.get("speaker_role")
            and prev.get("participant_id") == fr.get("participant_id")
        )
        should_merge = same_speaker and (
            len(prev_text) <= short_len
            or not re.search(r"[。！？\?]$", prev_text)
        )

        if should_merge:
            prev["text"] = f"{prev_text}{cur_text}"
            prev["source_segment_ids"] = list(dict.fromkeys((prev.get("source_segment_ids") or []) + (fr.get("source_segment_ids") or [])))
            prev["end_sec"] = fr.get("end_sec")
        else:
            out.append(dict(fr))
    return out


def split_long_fragments(
    fragments: list[dict[str, Any]],
    max_chars: int = 140,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    for fr in fragments:
        text = (fr.get("text") or "").strip()
        if len(text) <= max_chars:
            out.append(dict(fr))
            continue

        pieces = [p.strip() for p in _SPLIT_RE.split(text) if p and p.strip()]
        if not pieces:
            out.append(dict(fr))
            continue

        # 時間を等分配（時刻欠損時は None）
        start = fr.get("start_sec")
        end = fr.get("end_sec")
        total = len(pieces)
        for i, piece in enumerate(pieces):
            item = dict(fr)
            item["text"] = piece
            if start is not None and end is not None and end >= start:
                span = (end - start) / total
                item["start_sec"] = start + span * i
                item["end_sec"] = start + span * (i + 1)
            out.append(item)
    return out


def build_analysis_fragments(
    segments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    filtered, excluded = filter_noise_fragments(segments)
    merged = merge_short_fragments(filtered)
    split = split_long_fragments(merged)
    return split, {
        "candidate_count": len(segments),
        "filtered_count": len(filtered),
        "fragment_count": len(split),
        "excluded_counts": excluded,
        "excluded_count": sum(excluded.values()),
    }
