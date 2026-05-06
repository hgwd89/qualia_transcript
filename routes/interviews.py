import os
import json
import uuid
from datetime import datetime, timezone
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, jsonify, current_app)
from werkzeug.utils import secure_filename
import config
from models import db
from models.project import Project
from models.participant import Participant
from models.interview_flow import InterviewFlow, InterviewFlowQuestion
from models.interview import Interview, MediaFile, Transcription
from models.segment import Segment, UtteranceMapping
from models.segment_flag import SegmentFlag
from models.speaker_assignment import SpeakerAssignment
from models.analysis import AIAnalysis
from services.product_hint import lookup_product_hints, render_inline_hint

bp = Blueprint("interviews", __name__)

ALLOWED = config.ALLOWED_AUDIO_EXTENSIONS
FLAG_TYPES = ("favorite", "quote", "exclude", "needs_review")
SPEAKER_ROLES = ("moderator", "respondent", "observer", "unknown")


def _allowed(filename):
    return os.path.splitext(filename.lower())[1] in ALLOWED


def _latest_mapping_for_segment(seg: Segment):
    if not seg.utterance_mappings:
        return None
    return sorted(seg.utterance_mappings, key=lambda m: m.id or 0, reverse=True)[0]


def _is_unclassified_segment(seg: Segment, mapping=None) -> bool:
    mapping = mapping or _latest_mapping_for_segment(seg)
    if mapping is None:
        return True
    return bool(mapping.is_unclassified or mapping.question_id is None)


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
    speaker_assignments = (
        SpeakerAssignment.query
        .filter_by(interview_id=interview.id)
        .all()
    )
    speaker_assignment_map = {
        a.speaker_label: a
        for a in speaker_assignments
    }
    segment_flag_map = {
        seg.id: [f.flag_type for f in seg.segment_flags]
        for seg in interview.segments
    }
    unclassified_count = 0
    for seg in interview.segments:
        if seg.speaker_role != "respondent":
            continue
        if _is_unclassified_segment(seg):
            unclassified_count += 1

    semantic_analysis = (
        AIAnalysis.query
        .filter_by(interview_id=interview.id, analysis_type="semantic_clusters")
        .order_by(AIAnalysis.id.desc())
        .first()
    )
    semantic_payload = None
    semantic_error = None
    if semantic_analysis and semantic_analysis.content_json:
        try:
            semantic_payload = json.loads(semantic_analysis.content_json)
        except Exception:
            semantic_error = "意味クラスタ分析データの読み込みに失敗しました。"
    return render_template("interviews/detail.html", interview=interview,
                           project=interview.project,
                           segment_flag_map=segment_flag_map,
                           flag_types=FLAG_TYPES,
                           speaker_assignment_map=speaker_assignment_map,
                           unclassified_count=unclassified_count,
                           semantic_analysis=semantic_analysis,
                           semantic_payload=semantic_payload,
                           semantic_error=semantic_error)


@bp.route("/interviews/<int:interview_id>/unclassified")
def unclassified_review(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    segment_flag_map = {
        seg.id: [f.flag_type for f in seg.segment_flags]
        for seg in interview.segments
    }
    questions = []
    if interview.flow_id:
        questions = (
            InterviewFlowQuestion.query
            .join(InterviewFlowQuestion.section)
            .filter_by(flow_id=interview.flow_id)
            .order_by(InterviewFlowQuestion.seq.asc())
            .all()
        )

    review_rows = []
    excluded_rows = []
    respondent_segments = (
        Segment.query
        .filter_by(interview_id=interview.id, speaker_role="respondent")
        .order_by(Segment.seq.asc())
        .all()
    )

    for seg in respondent_segments:
        latest_mapping = _latest_mapping_for_segment(seg)
        if not _is_unclassified_segment(seg, latest_mapping):
            continue
        flags = segment_flag_map.get(seg.id, [])
        row = {
            "segment": seg,
            "flags": flags,
            "mapping": latest_mapping,
        }
        if "exclude" in flags:
            excluded_rows.append(row)
        else:
            review_rows.append(row)

    return render_template(
        "interviews/unclassified_review.html",
        interview=interview,
        project=interview.project,
        questions=questions,
        review_rows=review_rows,
        excluded_rows=excluded_rows,
        segment_flag_map=segment_flag_map,
        flag_types=FLAG_TYPES,
    )


@bp.route("/interviews/<int:interview_id>/speakers")
def speaker_review(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    participants = Participant.query.filter_by(project_id=interview.project_id).order_by(Participant.id.asc()).all()
    assignments = (
        SpeakerAssignment.query
        .filter_by(interview_id=interview.id)
        .all()
    )
    assignment_map = {a.speaker_label: a for a in assignments}

    distinct = (
        db.session.query(Segment.speaker_label)
        .filter(Segment.interview_id == interview.id)
        .filter(Segment.speaker_label.isnot(None))
        .distinct()
        .all()
    )
    labels = sorted([d[0] for d in distinct if d[0]])

    rows = []
    for label in labels:
        segs = (
            Segment.query
            .filter_by(interview_id=interview.id, speaker_label=label)
            .order_by(Segment.seq.asc())
            .all()
        )
        assignment = assignment_map.get(label)
        rows.append({
            "speaker_label": label,
            "segment_count": len(segs),
            "sample_text": (segs[0].text[:120] + "…") if segs and len(segs[0].text) > 120 else (segs[0].text if segs else ""),
            "assignment": assignment,
        })

    return render_template(
        "interviews/speaker_mapping.html",
        interview=interview,
        project=interview.project,
        participants=participants,
        rows=rows,
        speaker_roles=SPEAKER_ROLES,
    )


@bp.route("/api/interviews/<int:interview_id>/speakers/<string:speaker_label>", methods=["POST"])
def upsert_speaker_assignment(interview_id, speaker_label):
    interview = Interview.query.get_or_404(interview_id)
    label = (speaker_label or "").strip()
    if not label:
        return jsonify({"ok": False, "error": "speaker_label is required"}), 400

    exists = (
        Segment.query
        .filter_by(interview_id=interview.id, speaker_label=label)
        .first()
    )
    if not exists:
        return jsonify({"ok": False, "error": "speaker_label not found in interview"}), 400

    data = request.get_json(force=True, silent=True) or {}
    speaker_role = (data.get("speaker_role") or "unknown").strip()
    participant_id = data.get("participant_id")
    note = (data.get("note") or "").strip() or None

    if speaker_role not in SPEAKER_ROLES:
        return jsonify({"ok": False, "error": "invalid speaker_role"}), 400

    # respondent 以外は participant を紐づけない
    if speaker_role != "respondent":
        participant_id = None
    elif participant_id in ("", None):
        participant_id = None
    else:
        try:
            participant_id = int(participant_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid participant_id"}), 400
        participant = Participant.query.get(participant_id)
        if not participant or participant.project_id != interview.project_id:
            return jsonify({"ok": False, "error": "participant does not belong to project"}), 400

    assignment = SpeakerAssignment.query.filter_by(
        interview_id=interview.id, speaker_label=label
    ).first()
    created = False
    if not assignment:
        assignment = SpeakerAssignment(interview_id=interview.id, speaker_label=label)
        db.session.add(assignment)
        created = True

    assignment.speaker_role = speaker_role
    assignment.participant_id = participant_id
    assignment.note = note
    assignment.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({
        "ok": True,
        "created": created,
        "assignment": assignment.to_dict(),
    })


@bp.route("/api/interviews/<int:interview_id>/segments/<int:segment_id>/mapping", methods=["POST"])
def upsert_segment_mapping(interview_id, segment_id):
    interview = Interview.query.get_or_404(interview_id)
    seg = Segment.query.get_or_404(segment_id)
    if seg.interview_id != interview.id:
        return jsonify({"ok": False, "error": "segment does not belong to interview"}), 400

    data = request.get_json(force=True, silent=True) or {}
    raw_question_id = data.get("question_id")
    notes = (data.get("notes") or "").strip() or None
    mapped_by = "human"

    question_id = None
    if raw_question_id not in (None, "", "null"):
        try:
            question_id = int(raw_question_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid question_id"}), 400

        if not interview.flow_id:
            return jsonify({"ok": False, "error": "interview flow is not set"}), 400

        question = (
            InterviewFlowQuestion.query
            .join(InterviewFlowQuestion.section)
            .filter(InterviewFlowQuestion.id == question_id)
            .filter_by(flow_id=interview.flow_id)
            .first()
        )
        if not question:
            return jsonify({"ok": False, "error": "question does not belong to interview flow"}), 400

    mappings = (
        UtteranceMapping.query
        .filter_by(segment_id=seg.id)
        .order_by(UtteranceMapping.id.asc())
        .all()
    )
    primary = mappings[0] if mappings else None
    if primary is None:
        primary = UtteranceMapping(segment_id=seg.id)
        db.session.add(primary)
        mappings = [primary]

    for extra in mappings[1:]:
        db.session.delete(extra)

    primary.question_id = question_id
    primary.mapped_by = mapped_by
    primary.confidence = 1.0
    primary.is_unclassified = question_id is None
    primary.notes = notes or ("manual_unclassified" if question_id is None else "manual_assign")

    db.session.commit()
    return jsonify({
        "ok": True,
        "segment_id": seg.id,
        "mapping_id": primary.id,
        "question_id": primary.question_id,
        "is_unclassified": primary.is_unclassified,
        "mapped_by": primary.mapped_by,
        "confidence": primary.confidence,
    })


@bp.route("/api/segments/<int:segment_id>/flags", methods=["POST"])
def create_segment_flag(segment_id):
    seg = Segment.query.get_or_404(segment_id)
    data = request.get_json(force=True, silent=True) or {}
    flag_type = (data.get("flag_type") or "").strip()
    note = (data.get("note") or "").strip() or None
    if flag_type not in FLAG_TYPES:
        return jsonify({"ok": False, "error": "invalid flag_type"}), 400

    flag = SegmentFlag.query.filter_by(segment_id=seg.id, flag_type=flag_type).first()
    created = False
    if not flag:
        flag = SegmentFlag(segment_id=seg.id, flag_type=flag_type, note=note)
        db.session.add(flag)
        created = True
    else:
        flag.note = note
        flag.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    return jsonify({
        "ok": True,
        "segment_id": seg.id,
        "flag_type": flag_type,
        "created": created,
        "flag": flag.to_dict(),
    })


@bp.route("/api/segments/<int:segment_id>/flags/<string:flag_type>", methods=["DELETE"])
def delete_segment_flag(segment_id, flag_type):
    Segment.query.get_or_404(segment_id)
    if flag_type not in FLAG_TYPES:
        return jsonify({"ok": False, "error": "invalid flag_type"}), 400

    flag = SegmentFlag.query.filter_by(segment_id=segment_id, flag_type=flag_type).first()
    if not flag:
        return jsonify({"ok": True, "deleted": 0, "segment_id": segment_id, "flag_type": flag_type})

    db.session.delete(flag)
    db.session.commit()
    return jsonify({"ok": True, "deleted": 1, "segment_id": segment_id, "flag_type": flag_type})


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
