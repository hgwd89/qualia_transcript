"""
分析結果表示・プロジェクトレベル AI 分析トリガー。
"""
import json
from datetime import datetime, timezone
from flask import Blueprint, render_template, jsonify, redirect, request, url_for
from models import db
from models.project import Project
from models.interview_flow import InterviewFlowQuestion
from models.analysis import AIAnalysis
from services.analyzer import analyze_cross_participants, analyze_project_integrated

bp = Blueprint("analysis_view", __name__)

ALLOWED_REVIEW_STATUSES = {"reviewed", "approved", "rejected"}


def _has_required_per_question_trace(analysis: AIAnalysis) -> bool:
    if analysis.analysis_type != "per_question":
        return True
    try:
        source_segment_ids = json.loads(analysis.source_segment_ids or "[]")
    except (json.JSONDecodeError, TypeError):
        source_segment_ids = []
    try:
        content = json.loads(analysis.content_json or "{}")
    except (json.JSONDecodeError, TypeError):
        content = {}
    source_segment_quotes = content.get("source_segment_quotes") if isinstance(content, dict) else None
    return bool(source_segment_ids) and isinstance(source_segment_quotes, list) and bool(source_segment_quotes)


def _parse(a: AIAnalysis) -> dict:
    content = {}
    if a.content_json:
        try:
            content = json.loads(a.content_json)
        except (json.JSONDecodeError, TypeError):
            pass
    return {"obj": a, "content": content}


def _wants_json_response() -> bool:
    return bool(request.is_json or "application/json" in (request.headers.get("Accept") or ""))


@bp.route("/projects/<int:project_id>/analysis")
def index(project_id):
    project = Project.query.get_or_404(project_id)

    per_participant = [_parse(a) for a in AIAnalysis.query.filter_by(
        project_id=project_id, analysis_type="per_participant"
    ).order_by(AIAnalysis.created_at.desc()).all()]

    cross_participant = [_parse(a) for a in AIAnalysis.query.filter_by(
        project_id=project_id, analysis_type="cross_participant"
    ).order_by(AIAnalysis.created_at.desc()).all()]

    integrated_list = AIAnalysis.query.filter_by(
        project_id=project_id, analysis_type="integrated"
    ).order_by(AIAnalysis.created_at.desc()).all()
    integrated = _parse(integrated_list[0]) if integrated_list else None

    # フロー質問一覧（横断分析トリガー用）
    flows = project.interview_flows
    questions = []
    if flows:
        for section in flows[0].sections:
            for q in section.questions:
                questions.append({"obj": q, "section_title": section.title})

    return render_template(
        "analysis/index.html",
        project=project,
        per_participant=per_participant,
        cross_participant=cross_participant,
        integrated=integrated,
        questions=questions,
    )


@bp.route("/api/projects/<int:project_id>/analyze/cross/<int:question_id>", methods=["POST"])
def run_cross(project_id, question_id):
    try:
        analysis = analyze_cross_participants(project_id, question_id)
        return jsonify({"ok": True, "analysis_id": analysis.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/projects/<int:project_id>/analyze/integrated", methods=["POST"])
def run_integrated(project_id):
    try:
        analysis = analyze_project_integrated(project_id)
        return jsonify({"ok": True, "analysis_id": analysis.id,
                        "summary": analysis.summary_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/projects/<int:project_id>/analyses/<int:analysis_id>/status", methods=["POST"])
def update_analysis_status(project_id, analysis_id):
    analysis = AIAnalysis.query.filter_by(id=analysis_id, project_id=project_id).first()
    if not analysis:
        if _wants_json_response():
            return jsonify({"ok": False, "error": "analysis not found"}), 404
        return "analysis not found", 404

    payload = request.get_json(silent=True) if request.is_json else None
    status = ((payload or {}).get("status") if payload else None) or request.form.get("status") or ""
    status = status.strip().lower()
    if status not in ALLOWED_REVIEW_STATUSES:
        if _wants_json_response():
            return jsonify({"ok": False, "error": "invalid status"}), 400
        return "invalid status", 400

    if status == "approved" and not _has_required_per_question_trace(analysis):
        if _wants_json_response():
            return jsonify({"ok": False, "error": "per_question analysis requires source trace before approval"}), 400
        return "per_question analysis requires source trace before approval", 400

    analysis.status = status
    analysis.reviewed_at = datetime.now(timezone.utc)
    analysis.reviewed_by = "local_user"
    db.session.commit()

    if _wants_json_response():
        return jsonify({
            "ok": True,
            "analysis_id": analysis.id,
            "status": analysis.status,
            "reviewed_by": analysis.reviewed_by,
            "reviewed_at": analysis.reviewed_at.isoformat() if analysis.reviewed_at else None,
        })

    return redirect(url_for("analysis_view.index", project_id=project_id) + f"#analysis-{analysis.id}")
