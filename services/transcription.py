"""
音声文字起こしサービス。

- OpenAI transcription API
- ローカル faster-whisper（開発・フォールバック用）
"""
import os
import re
import json
import hashlib
from datetime import datetime, timezone
from typing import Any

from faster_whisper import WhisperModel
from openai import OpenAI

import config
from models import db
from models.interview import Transcription
from models.segment import Segment
from models.setting import AppSetting

_model_cache: dict = {}
_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？\?])")
_SOFT_BREAK_CHARS = ["。", "？", "?", "！", "、", ",", " "]
_DIARIZE_MODEL_NAMES = {"gpt-4o-transcribe-diarize"}
_SENTENCE_ENDINGS = ("。", "？", "?", "！", "!", "。\"", "？」", "。」")
_CONTINUATION_HEAD_CHARS = set("ぁぃぅぇぉゃゅょっゎーりるれろ")
_MODERATOR_HINTS = [
    "ですか",
    "ますか",
    "教えてください",
    "どれぐらい",
    "ちなみに",
    "大丈夫ですか",
    "聞こえます",
    "わかりました",
    "なるほど",
    "そっか",
]
_OPENAI_TRANSCRIBE_VERBATIM_PROMPT = (
    "逐語で書き起こしてください。"
    "フィラー（えー、あの、えっと、うーん等）、言い淀み、言い直し、重複、崩れた語尾を省略・要約・正規化しないでください。"
    "聞き取れない箇所は [inaudible] を使ってください。"
)


def _sanitize_error_message(message: str) -> str:
    """例外文中のAPIキーらしき文字列をマスクする。"""
    return _KEY_RE.sub("[REDACTED_KEY]", message or "")


def _normalize_provider(name: str) -> str:
    v = (name or "").strip().lower()
    if v in {"openai", "local_whisper"}:
        return v
    return "openai"


def get_transcription_provider() -> str:
    return _normalize_provider(config.TRANSCRIPTION_PROVIDER)


def get_fallback_provider() -> str:
    v = _normalize_provider(config.TRANSCRIPTION_FALLBACK_PROVIDER)
    return v if v != get_transcription_provider() else ""


def get_default_transcription_model(provider: str | None = None) -> str:
    p = _normalize_provider(provider or get_transcription_provider())
    if p == "openai":
        return config.OPENAI_TRANSCRIBE_MODEL
    return config.WHISPER_MODEL


def _openai_client() -> OpenAI:
    api_key = AppSetting.get("openai_api_key") or config.OPENAI_API_KEY
    return OpenAI(api_key=api_key)


def _write_raw_transcript_snapshot(
    transcription_id: int,
    interview_id: int,
    model_name: str,
    language: str,
    text: str,
) -> tuple[str, str]:
    """
    API返却の全文テキストを不変スナップショットとして保存する。
    既存ファイルは上書きしない（immutable）。
    """
    raw_dir = os.path.join(config.OUTPUT_DIR, "raw_transcripts")
    os.makedirs(raw_dir, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    payload = {
        "transcription_id": transcription_id,
        "interview_id": interview_id,
        "model": model_name,
        "language": language,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "sha256": digest,
        "text": text or "",
    }

    base_name = f"transcription_{transcription_id}_{ts}"
    suffix = 0
    while True:
        file_name = f"{base_name}.json" if suffix == 0 else f"{base_name}_{suffix}.json"
        abs_path = os.path.join(raw_dir, file_name)
        try:
            with open(abs_path, "x", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            rel_path = os.path.relpath(abs_path, config.OUTPUT_DIR)
            return rel_path, digest
        except FileExistsError:
            suffix += 1


def _get_model(model_name: str) -> WhisperModel:
    if model_name not in _model_cache:
        _model_cache[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _model_cache[model_name]


def _is_diarize_model(model_name: str) -> bool:
    return (model_name or "").strip() in _DIARIZE_MODEL_NAMES


def _to_dict(value: Any) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            dumped = value.to_dict()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass

    result: dict[str, Any] = {}
    for key in ["id", "speaker", "start", "end", "text", "segments", "duration"]:
        if hasattr(value, key):
            result[key] = getattr(value, key)
    return result


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _parse_diarized_segments(response: Any) -> list[dict]:
    """
    diarized_json レスポンスから [{speaker_label,start_sec,end_sec,text}] を抽出する。
    形式差分に耐えるため、dict/object の両方を許容する。
    """
    resp_dict = _to_dict(response)
    raw_segments = resp_dict.get("segments")
    if raw_segments is None and hasattr(response, "segments"):
        raw_segments = getattr(response, "segments")

    if not isinstance(raw_segments, list):
        return []

    parsed: list[dict] = []
    for idx, raw in enumerate(raw_segments):
        seg = _to_dict(raw)
        text = (seg.get("text") or "").strip()
        if not text:
            continue

        speaker = seg.get("speaker")
        speaker_label = str(speaker).strip() if speaker is not None else ""
        if not speaker_label:
            speaker_label = "SPEAKER_00"

        parsed.append({
            "speaker_label": speaker_label,
            "start_sec": _float_or_none(seg.get("start")),
            "end_sec": _float_or_none(seg.get("end")),
            "text": text,
            "seq_order": idx,
        })
    return parsed


def _is_sentence_complete(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if any(t.endswith(end) for end in _SENTENCE_ENDINGS):
        return True
    # 体言止めや文末助詞で終わる場合は未完扱い
    if t.endswith(("が", "けど", "ので", "から", "で", "と")):
        return False
    return False


def _starts_like_continuation(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    head = t[0]
    return head in _CONTINUATION_HEAD_CHARS


def merge_short_adjacent_segments(
    segments: list[dict],
    max_gap_sec: float = 1.2,
    short_chars: int = 12,
) -> tuple[list[dict], list[dict]]:
    """
    diarization 断片化対策:
    同一 speaker_label / speaker_role の短い連続セグメントを結合する。
    """
    if not segments:
        return [], []

    merged: list[dict] = []
    merge_examples: list[dict] = []

    for seg in segments:
        if not merged:
            merged.append(dict(seg))
            continue

        prev = merged[-1]
        same_speaker = (prev.get("speaker_label") == seg.get("speaker_label"))
        same_role = (prev.get("speaker_role") == seg.get("speaker_role"))

        prev_end = _float_or_none(prev.get("end_sec"))
        cur_start = _float_or_none(seg.get("start_sec"))
        gap = 0.0
        if prev_end is not None and cur_start is not None:
            gap = max(0.0, cur_start - prev_end)

        prev_text = (prev.get("text") or "").strip()
        cur_text = (seg.get("text") or "").strip()
        should_merge = (
            same_speaker
            and same_role
            and gap <= max_gap_sec
            and (
                len(prev_text) <= short_chars
                or len(cur_text) <= short_chars
                or not _is_sentence_complete(prev_text)
                or _starts_like_continuation(cur_text)
            )
        )

        if not should_merge:
            merged.append(dict(seg))
            continue

        merged_text = f"{prev_text}{cur_text}".strip()
        prev["text"] = merged_text
        if prev.get("start_sec") is None:
            prev["start_sec"] = seg.get("start_sec")
        prev["end_sec"] = seg.get("end_sec") if seg.get("end_sec") is not None else prev.get("end_sec")

        if len(merge_examples) < 20:
            merge_examples.append({
                "speaker_label": prev.get("speaker_label"),
                "speaker_role": prev.get("speaker_role"),
                "gap_sec": round(gap, 3),
                "before_prev": prev_text,
                "before_curr": cur_text,
                "after": merged_text,
            })

    return merged, merge_examples


def _split_long_chunk(text: str, max_chars: int) -> list[str]:
    """
    長文チャンクを max_chars 以内に緩く分割する。
    可能な限り句読点や空白で区切り、難しい場合は固定長で切る。
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remain = text
    while len(remain) > max_chars:
        window = remain[:max_chars]

        cut = -1
        for marker in _SOFT_BREAK_CHARS:
            pos = window.rfind(marker)
            cut = max(cut, pos + 1 if pos >= 0 else -1)

        # あまりに先頭寄りでしか切れない場合は固定長で分割
        if cut < int(max_chars * 0.5):
            cut = max_chars

        piece = remain[:cut].strip()
        if piece:
            chunks.append(piece)
        remain = remain[cut:].strip()

    if remain:
        chunks.append(remain)
    return chunks


def split_transcript_text(text: str, min_chars: int = 3, max_chars: int = 120) -> list[str]:
    """
    OpenAI transcription の全文を簡易的に複数セグメントへ分割する。
    - 区切り: 改行 / 。 / ？ / ? / ！
    - 短すぎる断片は前後と結合
    - 長すぎる断片は max_chars を目安に再分割
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []

    # 1) 改行 + 文末記号で一次分割
    raw_chunks: list[str] = []
    for line in normalized.split("\n"):
        line = line.strip()
        if not line:
            continue
        for part in _SENTENCE_SPLIT_RE.split(line):
            part = part.strip()
            if part:
                raw_chunks.append(part)

    if not raw_chunks:
        return []

    # 2) 短すぎる断片を前後へ結合
    merged: list[str] = []
    for part in raw_chunks:
        if len(part) < min_chars:
            if merged:
                merged[-1] = f"{merged[-1]}{part}"
            else:
                merged.append(part)
            continue

        if merged and len(merged[-1]) < min_chars:
            merged[-1] = f"{merged[-1]}{part}"
        else:
            merged.append(part)

    # 末尾が短すぎる場合は直前に結合
    if len(merged) >= 2 and len(merged[-1]) < min_chars:
        merged[-2] = f"{merged[-2]}{merged[-1]}"
        merged.pop()

    # 3) 長すぎる断片を分割
    final_chunks: list[str] = []
    for chunk in merged:
        final_chunks.extend(_split_long_chunk(chunk, max_chars=max_chars))

    return [c.strip() for c in final_chunks if c and c.strip()]


def _estimate_segment_times(chunks: list[str], duration_sec: float | None) -> list[tuple[float | None, float | None]]:
    """
    タイムスタンプが得られない OpenAI transcription 向けに、
    文字量比例で概算 start/end を付与する。
    """
    if not chunks:
        return []
    if not duration_sec or duration_sec <= 0:
        return [(None, None) for _ in chunks]

    total_chars = sum(max(len(c), 1) for c in chunks)
    cursor = 0.0
    out: list[tuple[float, float]] = []
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            end = duration_sec
        else:
            ratio = max(len(chunk), 1) / total_chars
            end = min(duration_sec, cursor + duration_sec * ratio)
        out.append((round(cursor, 3), round(end, 3)))
        cursor = end
    return out


def guess_speaker_role(text: str, allow_unknown: bool = False) -> str:
    """
    OpenAI transcription（単一話者ラベル）向けの暫定ロール推定。
    質問者/調査者らしい文は moderator、それ以外は respondent。
    """
    t = (text or "").strip()
    if not t:
        return "unknown" if allow_unknown else "respondent"

    if t.endswith("?") or t.endswith("？"):
        return "moderator"

    if any(hint in t for hint in _MODERATOR_HINTS):
        return "moderator"

    if allow_unknown and len(t) <= 2:
        return "unknown"

    return "respondent"


def infer_speaker_roles_from_diarized_segments(segments: list[dict]) -> dict[str, str]:
    """
    diarization で得た speaker_label ごとの役割を推定する。
    - モデレーターらしい発話が多い話者: moderator
    - それ以外: respondent
    - 判定不能: unknown
    """
    by_speaker: dict[str, dict[str, int]] = {}
    for seg in segments:
        label = seg.get("speaker_label") or "SPEAKER_00"
        text = seg.get("text") or ""
        role = guess_speaker_role(text, allow_unknown=True)

        if label not in by_speaker:
            by_speaker[label] = {"moderator": 0, "respondent": 0, "unknown": 0}
        by_speaker[label][role] = by_speaker[label].get(role, 0) + 1

    role_map: dict[str, str] = {}
    for label, counts in by_speaker.items():
        mod_n = counts.get("moderator", 0)
        resp_n = counts.get("respondent", 0)
        unk_n = counts.get("unknown", 0)

        if mod_n > resp_n:
            role_map[label] = "moderator"
        elif resp_n > mod_n:
            role_map[label] = "respondent"
        elif mod_n == 0 and resp_n == 0 and unk_n > 0:
            role_map[label] = "unknown"
        else:
            role_map[label] = "unknown"

    return role_map


def run_transcription(transcription_id: int) -> dict:
    """
    設定された provider に応じて文字起こしを実行するディスパッチャ。
    """
    provider = get_transcription_provider()

    if provider == "openai":
        try:
            return run_openai_transcription(transcription_id)
        except Exception:
            if get_fallback_provider() == "local_whisper":
                return run_local_whisper_transcription(transcription_id)
            raise

    return run_local_whisper_transcription(transcription_id)


def run_openai_transcription(transcription_id: int) -> dict:
    """
    OpenAI transcription API で文字起こしし、Segment を保存する。
    返却全文を簡易分割して複数Segment保存する。
    """
    tr = Transcription.query.get(transcription_id)
    if not tr:
        return {"error": "transcription not found"}

    tr.status = "running"
    tr.started_at = datetime.now(timezone.utc)
    tr.error_message = None
    db.session.commit()

    try:
        media = tr.media_file
        full_path = os.path.join(config.UPLOAD_DIR, media.stored_path)
        model_name = tr.whisper_model or config.OPENAI_TRANSCRIBE_MODEL

        client = _openai_client()
        req_kwargs = {
            "model": model_name,
            "language": tr.language or "ja",
        }
        if _is_diarize_model(model_name):
            req_kwargs["response_format"] = "diarized_json"
            req_kwargs["chunking_strategy"] = "auto"
        else:
            # diarizeモデルは prompt 非対応のため、通常モデル時のみ付与する
            req_kwargs["prompt"] = _OPENAI_TRANSCRIBE_VERBATIM_PROMPT

        with open(full_path, "rb") as audio_file:
            resp = client.audio.transcriptions.create(
                file=audio_file,
                **req_kwargs,
            )

        raw_text = getattr(resp, "text", "")
        if not raw_text:
            resp_dict = _to_dict(resp)
            raw_text = resp_dict.get("text") or ""

        text = raw_text.strip()
        if not text:
            raise RuntimeError("OpenAI transcription returned empty text")

        interview = media.interview
        raw_snapshot_path, raw_text_sha256 = _write_raw_transcript_snapshot(
            transcription_id=transcription_id,
            interview_id=interview.id,
            model_name=model_name,
            language=tr.language or "ja",
            text=raw_text,
        )
        seq = Segment.query.filter_by(interview_id=interview.id).count()

        diarized_segments: list[dict] = []
        if _is_diarize_model(model_name):
            diarized_segments = _parse_diarized_segments(resp)

        if diarized_segments:
            speaker_role_map = infer_speaker_roles_from_diarized_segments(diarized_segments)
            prepared: list[dict] = []
            for seg in diarized_segments:
                label = seg["speaker_label"]
                text_part = seg["text"]
                role = speaker_role_map.get(label, "unknown")
                if role == "unknown":
                    role = guess_speaker_role(text_part, allow_unknown=True)
                prepared.append({
                    "speaker_label": label,
                    "speaker_role": role,
                    "start_sec": seg.get("start_sec"),
                    "end_sec": seg.get("end_sec"),
                    "text": text_part,
                })

            merged_segments, _ = merge_short_adjacent_segments(prepared)
            for seg in merged_segments:
                db.session.add(Segment(
                    transcription_id=transcription_id,
                    interview_id=interview.id,
                    speaker_label=seg["speaker_label"],
                    speaker_role=seg["speaker_role"],
                    start_sec=seg.get("start_sec"),
                    end_sec=seg.get("end_sec"),
                    text=seg["text"],
                    seq=seq,
                ))
                seq += 1
            seg_count = len(merged_segments)
        else:
            # diarized_json が得られない場合は従来の簡易分割へフォールバック
            chunks = split_transcript_text(text)
            if not chunks:
                raise RuntimeError("OpenAI transcription split resulted in empty segments")

            timings = _estimate_segment_times(chunks, media.duration_sec)
            for idx, chunk in enumerate(chunks):
                start_sec, end_sec = timings[idx]
                db.session.add(Segment(
                    transcription_id=transcription_id,
                    interview_id=interview.id,
                    speaker_label="SPEAKER_00",
                    speaker_role=guess_speaker_role(chunk),
                    start_sec=start_sec,
                    end_sec=end_sec,
                    text=chunk,
                    seq=seq,
                ))
                seq += 1
            seg_count = len(chunks)

        word_count = len(text.split())
        tr.status = "done"
        tr.word_count = word_count
        tr.completed_at = datetime.now(timezone.utc)
        interview.status = "transcribed"
        db.session.commit()

        return {
            "segment_count": seg_count,
            "word_count": word_count,
            "raw_snapshot_path": raw_snapshot_path,
            "raw_text_sha256": raw_text_sha256,
        }

    except Exception as e:
        tr.status = "error"
        tr.error_message = _sanitize_error_message(str(e))
        db.session.commit()
        raise


def run_local_whisper_transcription(transcription_id: int) -> dict:
    """
    faster-whisper（ローカルCPU）で文字起こしし、Segment を保存する。
    """
    tr = Transcription.query.get(transcription_id)
    if not tr:
        return {"error": "transcription not found"}

    tr.status = "running"
    tr.started_at = datetime.now(timezone.utc)
    tr.error_message = None
    db.session.commit()

    try:
        media = tr.media_file
        full_path = os.path.join(config.UPLOAD_DIR, media.stored_path)

        model_name = tr.whisper_model or config.WHISPER_MODEL
        # OpenAI用モデル名が入っていた場合はローカルWhisperモデルにフォールバック
        if model_name.startswith("gpt-4o") or model_name == "whisper-1":
            model_name = config.WHISPER_MODEL

        model = _get_model(model_name)

        segments_gen, info = model.transcribe(
            full_path,
            language=tr.language or "ja",
            beam_size=1,
            vad_filter=False,
            word_timestamps=False,
        )

        interview = media.interview
        seq = Segment.query.filter_by(interview_id=interview.id).count()
        word_count = 0
        seg_count = 0

        speaker_labels: dict[str, str] = {}
        for seg in segments_gen:
            speaker = getattr(seg, "speaker", None) or "SPEAKER_00"
            if speaker not in speaker_labels:
                n = len(speaker_labels)
                speaker_labels[speaker] = f"SPEAKER_{n:02d}"

            s = Segment(
                transcription_id=transcription_id,
                interview_id=interview.id,
                speaker_label=speaker_labels[speaker],
                speaker_role="unknown",
                start_sec=seg.start,
                end_sec=seg.end,
                text=seg.text.strip(),
                seq=seq,
            )
            db.session.add(s)
            seq += 1
            seg_count += 1
            word_count += len(seg.text.split())

        tr.status = "done"
        tr.word_count = word_count
        tr.completed_at = datetime.now(timezone.utc)
        interview.status = "transcribed"
        db.session.commit()
        return {"segment_count": seg_count, "word_count": word_count}

    except Exception as e:
        tr.status = "error"
        tr.error_message = _sanitize_error_message(str(e))
        db.session.commit()
        raise


def auto_assign_speaker_roles(interview_id: int) -> None:
    """
    DI インタビュー向けヒューリスティック話者ロール自動割り当て。
    - 1話者: 全員 respondent
    - 2話者以上: 発話語数が最も少ない話者 = interviewer、それ以外 = respondent
    """
    segments = Segment.query.filter_by(interview_id=interview_id).all()
    if not segments:
        return

    # すでに手動設定済みの場合はスキップ
    manual_set = any(s.speaker_role != "unknown" for s in segments)
    if manual_set:
        return

    # 話者ごとの語数を集計
    word_counts: dict[str, int] = {}
    for seg in segments:
        label = seg.speaker_label or "SPEAKER_00"
        word_counts[label] = word_counts.get(label, 0) + len(seg.text.split())

    if len(word_counts) <= 1:
        for seg in segments:
            seg.speaker_role = "respondent"
    else:
        interviewer_label = min(word_counts, key=word_counts.get)
        for seg in segments:
            label = seg.speaker_label or "SPEAKER_00"
            seg.speaker_role = "interviewer" if label == interviewer_label else "respondent"

    db.session.commit()
