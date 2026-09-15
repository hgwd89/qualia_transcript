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
from services.upload_manager import save_and_register_media
from services.research_input_guard import (
    ResearchInputWriteBlocked,
    begin_interview_collection_write,
    begin_interview_input_write,
)

bp = Blueprint("interviews", __name__)

ALLOWED = config.ALLOWED_AUDIO_EXTENSIONS
FLAG_TYPES = ("favorite", "quote", "exclude", "needs_review")
SPEAKER_ROLES = ("moderator", "respondent", "observer", "unknown")


def _input_write_blocked_response(exc: ResearchInputWriteBlocked):
    return jsonify({
        "ok": False,
        "error": "処理中のジョブが分析入力を使用しているため、完了または失敗後に変更してください。",
        "active_job_ids": list(exc.active_job_ids),
    }), 409


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
    project = Project.query.get_or_404(project_id)
    participants = Participant.query.filter_by(project_id=project_id).all()
    flows = InterviewFlow.query.filter_by(project_id=project_id).all()

    if request.method == "POST":
        date_str = request.form.get("interview_date", "").strip()
        participant_id = request.form.get("participant_id") or None
        flow_id = request.form.get("flow_id") or None
        if participant_id is not None:
            participant = (
                Participant.query
                .filter_by(id=participant_id, project_id=project_id)
                .first_or_404()
            )
            participant_id = int(participant.id)
        if flow_id is not None:
            flow = (
                InterviewFlow.query
                .filter_by(id=flow_id, project_id=project_id)
                .first_or_404()
            )
            flow_id = int(flow.id)

        try:
            project = begin_interview_collection_write(
                project_id,
                affects_integrated_scope=participant_id is not None,
            )
        except ResearchInputWriteBlocked as exc:
            flash(
                "処理中のジョブがインタビュー集合を使用しているため、完了または失敗後に登録してください。"
                f" (job: {', '.join(str(value) for value in exc.active_job_ids)})",
                "error",
            )
            return render_template(
                "interviews/new.html",
                project=project,
                participants=participants,
                flows=flows,
            ), 409
        except ValueError:
            db.session.rollback()
            return "project not found", 404

        # The earlier lookups were only preflight validation. Re-resolve every
        # cross-table reference under the same BEGIN IMMEDIATE reservation that
        # serializes this collection write with durable job admission.
        if participant_id is not None:
            participant = (
                Participant.query
                .filter_by(id=int(participant_id), project_id=int(project.id))
                .first()
            )
            if participant is None:
                db.session.rollback()
                return "participant not found", 404
            participant_id = int(participant.id)
        if flow_id is not None:
            flow = (
                InterviewFlow.query
                .filter_by(id=int(flow_id), project_id=int(project.id))
                .first()
            )
            if flow is None:
                db.session.rollback()
                return "flow not found", 404
            flow_id = int(flow.id)

        interview = Interview(
            project_id=project_id,
            participant_id=participant_id,
            flow_id=flow_id,
            interview_date=datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else None,
            interviewer_name=request.form.get("interviewer_name", "").strip() or None,
            location=request.form.get("location", "").strip() or None,
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(interview)
        db.session.flush()

        file = request.files.get("audio_file")
        if file and file.filename and _allowed(file.filename):
            save_and_register_media(
                file,
                interview,
                original_filename=file.filename,
                mime_type=file.content_type,
            )
        else:
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


@bp.route("/interviews/<int:interview_id>/integrated-analysis/dry-run")
def integrated_analysis_preview(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    try:
        max_quotes = int(request.args.get("max_quotes", 20))
    except (TypeError, ValueError):
        max_quotes = 20
    max_quotes = max(1, min(max_quotes, 100))
    include_needs_review = request.args.get("include_needs_review") == "1"

    from services.integrated_analysis import run_integrated_interview_analysis

    result = run_integrated_interview_analysis(
        interview_id=interview.id,
        no_ai=True,
        save=False,
        max_quotes=max_quotes,
        include_needs_review=include_needs_review,
    )
    return render_template(
        "interviews/integrated_analysis_preview.html",
        interview=interview,
        project=interview.project,
        result=result,
        payload=result.get("payload") or {},
        max_quotes=max_quotes,
        include_needs_review=include_needs_review,
    )


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

    data = request.get_json(force=True, silent=True) or {}
    speaker_role = (data.get("speaker_role") or "unknown").strip()
    participant_id = data.get("participant_id")
    note = (data.get("note") or "").strip() or None

    if speaker_role not in SPEAKER_ROLES:
        return jsonify({"ok": False, "error": "invalid speaker_role"}), 400

    if participant_id in ("", None):
        participant_id = None
    else:
        try:
            participant_id = int(participant_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid participant_id"}), 400

    try:
        interview = begin_interview_input_write(interview.id)
    except ResearchInputWriteBlocked as exc:
        return _input_write_blocked_response(exc)
    except ValueError:
        return jsonify({"ok": False, "error": "interview not found"}), 404

    segments = (
        Segment.query
        .filter_by(interview_id=int(interview.id), speaker_label=label)
        .order_by(Segment.id.asc())
        .all()
    )
    if not segments:
        db.session.rollback()
        return jsonify({"ok": False, "error": "speaker_label not found in interview"}), 400

    if participant_id is not None:
        participant = db.session.get(Participant, int(participant_id))
        if not participant or int(participant.project_id) != int(interview.project_id):
            db.session.rollback()
            return jsonify({"ok": False, "error": "participant does not belong to project"}), 400

    assignment = SpeakerAssignment.query.filter_by(
        interview_id=int(interview.id), speaker_label=label
    ).first()
    created = False
    if not assignment:
        assignment = SpeakerAssignment(interview_id=int(interview.id), speaker_label=label)
        db.session.add(assignment)
        created = True

    assignment.speaker_role = speaker_role
    assignment.participant_id = participant_id
    assignment.note = note
    assignment.updated_at = datetime.now(timezone.utc)

    for segment in segments:
        segment.speaker_role = speaker_role
        segment.participant_id = participant_id

    db.session.commit()

    return jsonify({
        "ok": True,
        "created": created,
        "updated_segment_count": len(segments),
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

    try:
        interview = begin_interview_input_write(interview.id)
    except ResearchInputWriteBlocked as exc:
        return _input_write_blocked_response(exc)
    except ValueError:
        return jsonify({"ok": False, "error": "interview not found"}), 404

    seg = db.session.get(Segment, int(segment_id))
    if seg is None:
        db.session.rollback()
        return jsonify({"ok": False, "error": "segment not found"}), 404
    if int(seg.interview_id) != int(interview.id):
        db.session.rollback()
        return jsonify({"ok": False, "error": "segment does not belong to interview"}), 400

    if question_id is not None:
        if not interview.flow_id:
            db.session.rollback()
            return jsonify({"ok": False, "error": "interview flow is not set"}), 400

        question = (
            InterviewFlowQuestion.query
            .join(InterviewFlowQuestion.section)
            .filter(InterviewFlowQuestion.id == question_id)
            .filter_by(flow_id=interview.flow_id)
            .first()
        )
        if not question:
            db.session.rollback()
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
    interview = Interview.query.get_or_404(interview_id)
    seg = Segment.query.get_or_404(segment_id)
    if seg.interview_id != interview.id:
        return jsonify({"ok": False, "error": "segment does not belong to interview"}), 400

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not data:
        return jsonify({"ok": False, "error": "valid JSON object is required"}), 400
    if "speaker_role" not in data and "participant_id" not in data:
        return jsonify({"ok": False, "error": "no role fields supplied"}), 400

    requested_speaker_role = None
    if "speaker_role" in data:
        requested_speaker_role = str(data.get("speaker_role") or "").strip()
        if requested_speaker_role not in SPEAKER_ROLES:
            return jsonify({"ok": False, "error": "invalid speaker_role"}), 400

    participant_supplied = "participant_id" in data
    requested_participant_id = data.get("participant_id") if participant_supplied else None
    if participant_supplied and requested_participant_id not in (None, ""):
        try:
            requested_participant_id = int(requested_participant_id)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid participant_id"}), 400
    elif participant_supplied:
        requested_participant_id = None

    try:
        interview = begin_interview_input_write(interview.id)
    except ResearchInputWriteBlocked as exc:
        return _input_write_blocked_response(exc)
    except ValueError:
        return jsonify({"ok": False, "error": "interview not found"}), 404

    seg = db.session.get(Segment, int(segment_id))
    if seg is None:
        db.session.rollback()
        return jsonify({"ok": False, "error": "segment not found"}), 404
    if int(seg.interview_id) != int(interview.id):
        db.session.rollback()
        return jsonify({"ok": False, "error": "segment does not belong to interview"}), 400

    speaker_role = seg.speaker_role or "unknown"
    if requested_speaker_role is not None:
        speaker_role = requested_speaker_role

    participant_id = seg.participant_id
    if participant_supplied:
        participant_id = requested_participant_id
        if participant_id is not None:
            participant = db.session.get(Participant, int(participant_id))
            if not participant or int(participant.project_id) != int(interview.project_id):
                db.session.rollback()
                return jsonify({"ok": False, "error": "participant does not belong to project"}), 400

    seg.speaker_role = speaker_role
    seg.participant_id = participant_id
    db.session.commit()
    return jsonify({
        "ok": True,
        "segment_id": seg.id,
        "speaker_role": seg.speaker_role,
        "participant_id": seg.participant_id,
    })


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
        return jsonify({
            "ok": False,
            "segment_id": seg.id,
            "hints": [],
            "error": "商品候補の取得に失敗しました",
        }), 200
