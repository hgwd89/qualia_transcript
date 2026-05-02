from flask import Blueprint, jsonify, request
from models.interview import Interview
from services.analyzer import analyze_interview_summary, analyze_per_question

bp = Blueprint("analyze", __name__)


@bp.route("/api/interviews/<int:interview_id>/analyze", methods=["POST"])
def analyze_interview(interview_id):
    try:
        analysis = analyze_interview_summary(interview_id)
        return jsonify({"ok": True, "analysis_id": analysis.id,
                        "summary": analysis.summary_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/interviews/<int:interview_id>/analyze/question/<int:question_id>",
          methods=["POST"])
def analyze_question(interview_id, question_id):
    try:
        analysis = analyze_per_question(interview_id, question_id)
        return jsonify({"ok": True, "analysis_id": analysis.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
