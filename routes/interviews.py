import os
import uuid
from datetime import datetime, timezone
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, current_app)
from werkzeug.utils import secure_filename
import config
from models import db
from models.project import Project
from models.participant import Participant
from models.interview_flow import InterviewFlow
from models.interview import Interview, MediaFile, Transcription
from models.segment import Segment, UtteranceMapping
from services.product_hint import lookup_product_hints, render_inline_hint

bp = Blueprint("interviews", __name__)

ALLOWED = config.ALLOWED_AUDIO_EXTENSIONS


def _allowed(filename):
    return os.path.splitext(filename.lower())[1] in ALLOWED


@bp.route("/projects/<int:project_id>/interviews/new", methods=["GET", "POST"])
def new(project_id):
    project      = Project.query.get_or_404(project_id)
    participants = Participant.query.filter_by(project_id=project_id).all()
    flows        = InterviewFlow.query.filter_by(project_id=project_id).all()

    if request.method == "POST":
        date_str = request.form.get("interview_date", "").strip()
        interview = Interview(
            project_id=project_id,
            participant_id=request.form.get("participant_id") or None,
            flow_id=request.form.get("flow_id") or None,
            interview_date=datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None,
            interviewer_name=request.form.get("interviewer_name", "").strip() or None,
            location=request.form.get("location", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(interview)
        db.session.flush()

        # 音声ファイルのアップロード
        file = request.files.get("audio_file")
        if file and file.filename and _allowed(file.filename):
            ext      = os.path.splitext(secure_filename(file.filename))[1].lower()
            stored   = f"{uuid.uuid4().hex}{ext}"
            save_dir = os.path.join(config.UPLOAD_DIR, str(interview.id))
            os.makedirs(save_dir, exist_ok=True)
            full_path = os.path.join(save_dir, stored)
            file.save(full_path)
            rel_path  = os.path.join(str(interview.id), stored)

            media = MediaFile(
                interview_id=interview.id,
                original_filename=file.filename,
                stored_path=rel_path,
                file_type="audio" if ext in {".mp3",".m4a",".wav",".ogg",".flac"} else "video",
                mime_type=file.content_type,
            )
            db.session.add(media)

        db.session.commit()
        flash("インタビューを登録しました", "success")
        return redirect(url_for("interviews.detail", interview_id=interview.id))

    return render_template("interviews/new.html", project=project,
                           participants=participants, flows=flows)


@bp.route("/interviews/<int:interview_id>")
def detail(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    return render_template("interviews/detail.html", interview=interview,
                           project=interview.project)


@bp.route("/interviews/<int:interview_id>/segments/<int:segment_id>/role", methods=["POST"])
def update_segment_role(interview_id, segment_id):
    seg = Segment.query.get_or_404(segment_id)
    data = request.get_json(force=True)
    seg.speaker_role    = data.get("speaker_role", seg.speaker_role)
    seg.participant_id  = data.get("participant_id") or None
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/interviews/<int:interview_id>/segments/<int:segment_id>/product-hint")
def segment_product_hint(interview_id, segment_id):
    seg = Segment.query.get_or_404(segment_id)
    if seg.interview_id != interview_id:
        return jsonify({"error": "segment does not belong to interview"}), 400

    try:
        profile = seg.interview.project.glossary_profile if seg.interview and seg.interview.project else None
        hints = lookup_product_hints(seg.text, max_hints=1, glossary_profile=profile)
        inline_hint = render_inline_hint(hints[0]) if hints else ""
        return jsonify({
            "ok": True,
            "segment_id": seg.id,
            "hints": hints,
            "inline_hint": inline_hint,
        })
    except Exception:
        # UI利用時の検索失敗は致命にしない
        return jsonify({
            "ok": False,
            "segment_id": seg.id,
            "hints": [],
            "error": "商品候補の取得に失敗しました",
        }), 200
