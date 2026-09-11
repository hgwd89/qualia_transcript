from datetime import datetime, timezone

from flask import Blueprint, jsonify

from models import db
from models.interview import Interview
from models.interview_flow import InterviewFlowQuestion
from models.processing_job import ProcessingJob
from services.job_admission import admit_processing_job
from services.processing_jobs import launch_job_worker

bp = Blueprint("analyze", __name__)


def _conflict_response(conflict: ProcessingJob):
    return jsonify({
        "ok": False,
        "error": "別の処理ジョブが実行中です。完了または失敗後に再実行してください。",
        "conflicting_job": conflict.to_dict(),
    }), 409


def _queued_response(job: ProcessingJob, *, created: bool, worker_pid=None):
    return jsonify({
        "ok": True,
        "queued": True,
        "created": created,
        "job_id": job.id,
        "job_type": job.job_type,
        "question_id": job.question_id,
        "job_status": job.status,
        "worker_pid": worker_pid if created else job.worker_pid,
    }), 202


def _launch_or_fail(job: ProcessingJob):
    try:
        return launch_job_worker(job.id), None
    except Exception as exc:
        db.session.rollback()
        current = db.session.get(ProcessingJob, job.id)
        if current:
            current.status = "failed"
            current.error_message = f"worker launch failed: {exc}"[:4000]
            current.finished_at = datetime.now(timezone.utc)
            current.worker_pid = None
            db.session.add(current)
            db.session.commit()
        return None, exc


def _queue_analysis_job(
    project_id: int,
    job_type: str,
    *,
    interview_id: int | None = None,
    question_id: int | None = None,
):
    admission = admit_processing_job(
        project_id,
        job_type,
        interview_id,
        question_id=question_id,
    )
    if admission.conflict_job_id:
        conflict = db.session.get(ProcessingJob, admission.conflict_job_id)
        return _conflict_response(conflict)
    if admission.error:
        return jsonify({"ok": False, "error": admission.error}), 409

    job = db.session.get(ProcessingJob, admission.job_id)
    if not job:
        return jsonify({"ok": False, "error": "processing job not found after admission"}), 500
    if not admission.created:
        return _queued_response(job, created=False)

    pid, launch_error = _launch_or_fail(job)
    if launch_error is not None:
        return jsonify({"ok": False, "job_id": job.id, "error": str(launch_error)}), 500
    return _queued_response(job, created=True, worker_pid=pid)


@bp.route("/api/interviews/<int:interview_id>/analyze", methods=["POST"])
def analyze_interview(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    if interview.status not in {"mapped", "analyzed", "done"}:
        return jsonify({"ok": False, "error": "先にマッピングを完了してください"}), 409
    return _queue_analysis_job(
        int(interview.project_id),
        "analyze",
        interview_id=interview_id,
    )


@bp.route("/api/interviews/<int:interview_id>/analyze/question/<int:question_id>", methods=["POST"])
def analyze_question(interview_id, question_id):
    interview = Interview.query.get_or_404(interview_id)
    question = InterviewFlowQuestion.query.get_or_404(question_id)
    if interview.status not in {"mapped", "analyzed", "done"}:
        return jsonify({"ok": False, "error": "先にマッピングを完了してください"}), 409
    if not interview.flow_id or question.section.flow_id != interview.flow_id:
        return jsonify({"ok": False, "error": "質問がこのインタビューのフローに含まれていません"}), 404
    return _queue_analysis_job(
        int(interview.project_id),
        "analyze_question",
        interview_id=interview_id,
        question_id=question_id,
    )
