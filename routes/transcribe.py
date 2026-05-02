from flask import Blueprint, jsonify, request
import config
from models import db
from models.interview import Interview, MediaFile, Transcription
from services.transcription import run_transcription
from services.mapper import run_mapping

bp = Blueprint("transcribe", __name__)


@bp.route("/api/interviews/<int:interview_id>/transcribe", methods=["POST"])
def start_transcription(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    if not interview.media_files:
        return jsonify({"error": "音声ファイルが登録されていません"}), 400

    media = interview.media_files[-1]

    # 既存の完了済み transcription があればスキップ
    existing = Transcription.query.filter_by(
        media_file_id=media.id, status="done"
    ).first()
    if existing:
        return jsonify({"message": "既に文字起こし済みです", "transcription_id": existing.id})

    tr = Transcription(
        media_file_id=media.id,
        whisper_model=config.WHISPER_MODEL,
        language="ja",
        status="pending",
    )
    db.session.add(tr)
    db.session.commit()

    try:
        result = run_transcription(tr.id)
        return jsonify({"ok": True, "transcription_id": tr.id, **result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/interviews/<int:interview_id>/status")
def get_status(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    tr = None
    if interview.media_files:
        tr = Transcription.query.filter_by(
            media_file_id=interview.media_files[-1].id
        ).order_by(Transcription.id.desc()).first()

    return jsonify({
        "interview_status": interview.status,
        "transcription_status": tr.status if tr else None,
        "segment_count": len(interview.segments),
    })


@bp.route("/api/interviews/<int:interview_id>/map", methods=["POST"])
def start_mapping(interview_id):
    try:
        count = run_mapping(interview_id)
        return jsonify({"ok": True, "mapped_count": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
