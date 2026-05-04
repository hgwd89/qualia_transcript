"""
faster-whisper による音声文字起こし＋話者ロール自動推定。
"""
import os
from datetime import datetime, timezone
from faster_whisper import WhisperModel
import config
from models import db
from models.interview import Interview, MediaFile, Transcription
from models.segment import Segment

_model_cache: dict = {}


def _get_model(model_name: str) -> WhisperModel:
    if model_name not in _model_cache:
        _model_cache[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    return _model_cache[model_name]


def run_transcription(transcription_id: int) -> dict:
    """
    Transcription レコードを処理し、Segment を生成して返す。
    戻り値: {"segment_count": int, "word_count": int}
    """
    tr = Transcription.query.get(transcription_id)
    if not tr:
        return {"error": "transcription not found"}

    tr.status     = "running"
    tr.started_at = datetime.now(timezone.utc)
    db.session.commit()

    try:
        media     = tr.media_file
        full_path = os.path.join(config.UPLOAD_DIR, media.stored_path)
        model     = _get_model(tr.whisper_model or config.WHISPER_MODEL)

        segments_gen, info = model.transcribe(
            full_path,
            language=tr.language or "ja",
            beam_size=1,
            vad_filter=False,
            word_timestamps=False,
        )

        interview  = media.interview
        seq        = Segment.query.filter_by(interview_id=interview.id).count()
        word_count = 0
        seg_count  = 0

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
            seq        += 1
            seg_count  += 1
            word_count += len(seg.text.split())

        tr.status        = "done"
        tr.word_count    = word_count
        tr.completed_at  = datetime.now(timezone.utc)
        interview.status = "transcribed"
        db.session.commit()
        return {"segment_count": seg_count, "word_count": word_count}

    except Exception as e:
        tr.status        = "error"
        tr.error_message = str(e)
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
