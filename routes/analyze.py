from datetime import datetime, timezone

from flask import Blueprint, jsonify

from models import db
from models.interview import Interview
from models.processing_job import ProcessingJob
from services.analyzer import analyze_per_question
from services.processing_jobs import create_or_get_active_job, launch_job_worker

bp = Blueprint("analyze", __name__)


@bp.route("/api/interviews/<int:interview_id>/analyze", methods=["POST"])
def analyze_interview(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    if interview.status not in {"mapped", "analyzed", "done"}:
        return jsonify({"error": "先にマッピングを完了してください"}), 409

    job, created = create_or_get_active_job(
        project_id=interview.project_id,
        interview_id=interview.id,
        job_type="analyze",
    )
    if not created:
        return jsonify({"ok": True, "queued": True, "created": False, "job_id": job.id, "job_status": job.status}), 202

    try:
        pid = launch_job_worker(job.id)
    except Exception as exc:
        db.session.rollback()
        job = db.session.get(ProcessingJob, job.id)
        job.status = "failed"
        job.error_message = f"worker launch failed: {exc}"[:4000]
        job.finished_at = datetime.now(timezone.utc)
        db.session.add(job)
        db.session.commit()
        return jsonify({"ok": False, "job_id": job.id, "error": str(exc)}), 500

    return jsonify({
        "ok": True,
        "queued": True,
        "created": True,
        "job_id": job.id,
        "job_status": job.status,
        "worker_pid": pid,
    }), 202


@bp.route("/api/interviews/<int:interview_id>/analyze/question/<int:question_id>", methods=["POST"])
def analyze_question(interview_id, question_id):
    try:
        analysis = analyze_per_question(interview_id, question_id)
        return jsonify({"ok": True, "analysis_id": analysis.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
