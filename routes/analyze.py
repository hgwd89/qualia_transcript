from datetime import datetime, timezone

from flask import Blueprint, jsonify

from models import db
from models.interview import Interview
from models.processing_job import ProcessingJob
from services.analyzer import analyze_per_question
from services.job_admission import admit_processing_job
from services.processing_jobs import launch_job_worker

bp = Blueprint("analyze", __name__)


def _conflict_response(conflict: ProcessingJob):
    return jsonify({
        "ok": False,
        "error": "別の処理ジョブが実行中です。完了または失敗後に再実行してください。",
        "conflicting_job": conflict.to_dict(),
    }), 409


@bp.route("/api/interviews/<int:interview_id>/analyze", methods=["POST"])
def analyze_interview(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    project_id = int(interview.project_id)
    if interview.status not in {"mapped", "analyzed", "done"}:
        return jsonify({"error": "先にマッピングを完了してください"}), 409

    admission = admit_processing_job(project_id, "analyze", interview_id)
    if admission.conflict_job_id:
        conflict = db.session.get(ProcessingJob, admission.conflict_job_id)
        return _conflict_response(conflict)
    if admission.error:
        return jsonify({"ok": False, "error": admission.error}), 409

    job = db.session.get(ProcessingJob, admission.job_id)
    if not job:
        return jsonify({"ok": False, "error": "processing job not found after admission"}), 500
    if not admission.created:
        return jsonify({
            "ok": True,
            "queued": True,
            "created": False,
            "job_id": job.id,
            "job_status": job.status,
            "worker_pid": job.worker_pid,
        }), 202

    try:
        pid = launch_job_worker(job.id)
    except Exception as exc:
        db.session.rollback()
        job = db.session.get(ProcessingJob, job.id)
        job.status = "failed"
        job.error_message = f"worker launch failed: {exc}"[:4000]
        job.finished_at = datetime.now(timezone.utc)
        job.worker_pid = None
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
