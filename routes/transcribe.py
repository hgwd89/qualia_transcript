from datetime import datetime, timezone

from flask import Blueprint, jsonify

from models import db
from models.interview import Interview, Transcription
from models.processing_job import ProcessingJob
from models.project import Project
from services.job_conflicts import find_conflicting_active_job
from services.job_recovery import recover_stale_jobs
from services.processing_jobs import (
    create_or_get_active_job,
    launch_job_worker,
    retry_failed_job,
)

bp = Blueprint("transcribe", __name__)


def _launch_or_fail(job: ProcessingJob):
    try:
        pid = launch_job_worker(job.id)
        return None, pid
    except Exception as exc:
        db.session.rollback()
        job = db.session.get(ProcessingJob, job.id)
        job.status = "failed"
        job.error_message = f"worker launch failed: {exc}"[:4000]
        job.finished_at = datetime.now(timezone.utc)
        db.session.add(job)
        db.session.commit()
        return str(exc), None


def _queued_response(job: ProcessingJob, created: bool, pid: int | None = None):
    return jsonify({
        "ok": True,
        "queued": True,
        "created": created,
        "job_id": job.id,
        "job_type": job.job_type,
        "job_status": job.status,
        "worker_pid": pid or job.worker_pid,
    }), 202


def _conflict_response(conflict: ProcessingJob):
    return jsonify({
        "ok": False,
        "error": "別の処理ジョブが実行中です。完了または失敗後に再実行してください。",
        "conflicting_job": conflict.to_dict(),
    }), 409


@bp.route("/api/interviews/<int:interview_id>/transcribe", methods=["POST"])
def start_transcription(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    if not interview.media_files:
        return jsonify({"error": "音声ファイルが登録されていません"}), 400

    media = interview.media_files[-1]
    existing = (
        Transcription.query
        .filter_by(media_file_id=media.id, status="done")
        .order_by(Transcription.id.desc())
        .first()
    )
    if existing:
        return jsonify({
            "ok": True,
            "already_done": True,
            "transcription_id": existing.id,
            "segment_count": len(interview.segments),
        })

    conflict = find_conflicting_active_job(interview.project_id, "transcribe", interview.id)
    if conflict:
        return _conflict_response(conflict)

    job, created = create_or_get_active_job(
        project_id=interview.project_id,
        interview_id=interview.id,
        job_type="transcribe",
    )
    if not created:
        return _queued_response(job, False)

    error, pid = _launch_or_fail(job)
    if error:
        return jsonify({"ok": False, "job_id": job.id, "error": error}), 500
    return _queued_response(job, True, pid)


@bp.route("/api/interviews/<int:interview_id>/map", methods=["POST"])
def start_mapping(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    if interview.status == "pending" or not interview.segments:
        return jsonify({"error": "先に文字起こしを完了してください"}), 409

    conflict = find_conflicting_active_job(interview.project_id, "map", interview.id)
    if conflict:
        return _conflict_response(conflict)

    job, created = create_or_get_active_job(
        project_id=interview.project_id,
        interview_id=interview.id,
        job_type="map",
    )
    if not created:
        return _queued_response(job, False)

    error, pid = _launch_or_fail(job)
    if error:
        return jsonify({"ok": False, "job_id": job.id, "error": error}), 500
    return _queued_response(job, True, pid)


@bp.route("/api/projects/<int:project_id>/process/all", methods=["POST"])
def process_all(project_id):
    project = Project.query.get_or_404(project_id)
    conflict = find_conflicting_active_job(project.id, "project_pipeline", None)
    if conflict:
        return _conflict_response(conflict)

    job, created = create_or_get_active_job(
        project_id=project.id,
        interview_id=None,
        job_type="project_pipeline",
    )
    if not created:
        return _queued_response(job, False)

    error, pid = _launch_or_fail(job)
    if error:
        return jsonify({"ok": False, "job_id": job.id, "error": error}), 500
    return _queued_response(job, True, pid)


@bp.route("/api/interviews/<int:interview_id>/status")
def get_status(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    recover_stale_jobs(project_id=interview.project_id)

    tr = None
    if interview.media_files:
        tr = (
            Transcription.query
            .filter_by(media_file_id=interview.media_files[-1].id)
            .order_by(Transcription.id.desc())
            .first()
        )

    latest_job = (
        ProcessingJob.query
        .filter_by(interview_id=interview.id)
        .order_by(ProcessingJob.id.desc())
        .first()
    )
    return jsonify({
        "interview_status": interview.status,
        "transcription_status": tr.status if tr else None,
        "segment_count": len(interview.segments),
        "segment_roles": [
            {
                "segment_id": seg.id,
                "speaker_role": seg.speaker_role or "unknown",
                "participant_id": seg.participant_id,
            }
            for seg in interview.segments
        ],
        "latest_job": latest_job.to_dict() if latest_job else None,
    })


@bp.route("/api/processing-jobs/<int:job_id>")
def processing_job_status(job_id):
    job = ProcessingJob.query.get_or_404(job_id)
    recover_stale_jobs(project_id=job.project_id)
    job = db.session.get(ProcessingJob, job_id)
    return jsonify({"ok": True, "job": job.to_dict()})


@bp.route("/api/processing-jobs/<int:job_id>/retry", methods=["POST"])
def retry_processing_job(job_id):
    job = ProcessingJob.query.get_or_404(job_id)
    conflict = find_conflicting_active_job(job.project_id, job.job_type, job.interview_id)
    if conflict:
        return _conflict_response(conflict)

    try:
        retry_failed_job(job)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409

    error, pid = _launch_or_fail(job)
    if error:
        return jsonify({"ok": False, "job_id": job.id, "error": error}), 500
    return _queued_response(job, True, pid)
