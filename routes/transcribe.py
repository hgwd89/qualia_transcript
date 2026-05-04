from flask import Blueprint, jsonify, request
import config
from models import db
from models.interview import Interview, MediaFile, Transcription
from models.interview_flow import InterviewFlow
from services.transcription import (
    run_transcription,
    auto_assign_speaker_roles,
    get_default_transcription_model,
)
from services.mapper import run_mapping
from services.analyzer import analyze_interview_summary

bp = Blueprint("transcribe", __name__)


@bp.route("/api/interviews/<int:interview_id>/transcribe", methods=["POST"])
def start_transcription(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    if not interview.media_files:
        return jsonify({"error": "音声ファイルが登録されていません"}), 400

    media = interview.media_files[-1]

    existing = Transcription.query.filter_by(
        media_file_id=media.id, status="done"
    ).first()
    if existing:
        return jsonify({"message": "既に文字起こし済みです", "transcription_id": existing.id})

    tr = Transcription(
        media_file_id=media.id,
        whisper_model=get_default_transcription_model(),
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


@bp.route("/api/projects/<int:project_id>/process/all", methods=["POST"])
def process_all(project_id):
    """
    プロジェクト内の全インタビューを一括処理する。
    処理順: 文字起こし → 話者ロール自動割り当て → マッピング → 考察生成
    """
    from models.project import Project
    project = Project.query.get_or_404(project_id)
    interviews = [iv for iv in project.interviews if iv.status != "error"]

    # インタビューにフローが未設定の場合、プロジェクトの最初のフローを使用
    first_flow = project.interview_flows[0] if project.interview_flows else None

    results = []
    for iv in interviews:
        iv_result = {"interview_id": iv.id, "steps": []}

        # フロー未設定の場合は最初のフローを自動設定
        if not iv.flow_id and first_flow:
            iv.flow_id = first_flow.id
            db.session.commit()

        # ① 文字起こし
        if iv.status == "pending":
            if not iv.media_files:
                iv_result["steps"].append({"step": "transcribe", "result": "skipped (no media)"})
            else:
                media = iv.media_files[-1]
                existing_done = Transcription.query.filter_by(
                    media_file_id=media.id, status="done"
                ).first()
                if not existing_done:
                    tr = Transcription(
                        media_file_id=media.id,
                        whisper_model=get_default_transcription_model(),
                        language="ja",
                        status="pending",
                    )
                    db.session.add(tr)
                    db.session.commit()
                    try:
                        res = run_transcription(tr.id)
                        iv_result["steps"].append({"step": "transcribe", "result": res})
                    except Exception as e:
                        iv_result["steps"].append({"step": "transcribe", "error": str(e)})
                        results.append(iv_result)
                        continue
                else:
                    iv.status = "transcribed"
                    db.session.commit()

        # ② 話者ロール自動割り当て
        if iv.status == "transcribed":
            try:
                auto_assign_speaker_roles(iv.id)
                iv_result["steps"].append({"step": "auto_roles", "result": "ok"})
            except Exception as e:
                iv_result["steps"].append({"step": "auto_roles", "error": str(e)})

        # ③ マッピング
        if iv.status in ("transcribed",):
            try:
                count = run_mapping(iv.id)
                iv_result["steps"].append({"step": "mapping", "result": count})
            except Exception as e:
                iv_result["steps"].append({"step": "mapping", "error": str(e)})

        # ④ 考察生成
        if iv.status in ("mapped",):
            try:
                analysis = analyze_interview_summary(iv.id)
                iv_result["steps"].append({"step": "analyze", "result": analysis.id})
            except Exception as e:
                iv_result["steps"].append({"step": "analyze", "error": str(e)})

        results.append(iv_result)

    return jsonify({"ok": True, "results": results})
