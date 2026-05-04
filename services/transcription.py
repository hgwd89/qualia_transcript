"""
音声文字起こしサービス。

- OpenAI transcription API
- ローカル faster-whisper（開発・フォールバック用）
"""
import os
import re
from datetime import datetime, timezone

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


def _get_model(model_name: str) -> WhisperModel:
    if model_name not in _model_cache:
        _model_cache[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _model_cache[model_name]


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
        with open(full_path, "rb") as audio_file:
            resp = client.audio.transcriptions.create(
                model=model_name,
                file=audio_file,
                language=tr.language or "ja",
            )

        text = getattr(resp, "text", "") or ""
        text = text.strip()
        if not text:
            raise RuntimeError("OpenAI transcription returned empty text")

        interview = media.interview
        seq = Segment.query.filter_by(interview_id=interview.id).count()
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
                speaker_role="respondent",
                start_sec=start_sec,
                end_sec=end_sec,
                text=chunk,
                seq=seq,
            ))
            seq += 1

        word_count = len(text.split())
        tr.status = "done"
        tr.word_count = word_count
        tr.completed_at = datetime.now(timezone.utc)
        interview.status = "transcribed"
        db.session.commit()

        return {"segment_count": len(chunks), "word_count": word_count}

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
