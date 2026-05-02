"""
分析結果表示・プロジェクトレベル AI 分析トリガー。
"""
import json
from flask import Blueprint, render_template, jsonify, request
from models.project import Project
from models.interview_flow import InterviewFlowQuestion
from models.analysis import AIAnalysis
from services.analyzer import analyze_cross_participants, analyze_project_integrated

bp = Blueprint("analysis_view", __name__)


def _parse(a: AIAnalysis) -> dict:
    content = {}
    if a.content_json:
        try:
            content = json.loads(a.content_json)
        except (json.JSONDecodeError, TypeError):
            pass
    return {"obj": a, "content": content}


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
