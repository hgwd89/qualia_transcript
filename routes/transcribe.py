from flask import Blueprint, jsonify

from models import db
from models.interview import Interview, Transcription
from models.processing_job import ProcessingJob
from models.project import Project
from services.job_admission import admit_processing_job, admit_retry_job
from services.job_recovery import recover_stale_jobs
from services.media_source import canonical_media_for_interview
from services.worker_launch_guard import launch_job_or_preserve_active

bp = Blueprint("transcribe", __name__)


def _launch_or_fail(job: ProcessingJob):
    pid, launch_error = launch_job_or_preserve_active(job)
    return (str(launch_error) if launch_error is not None else None), pid


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


def _resolve_admission(admission):
    if admission.conflict_job_id:
        conflict = db.session.get(ProcessingJob, admission.conflict_job_id)
        return None, _conflict_response(conflict)
    if admission.error:
        return None, (jsonify({"ok": False, "error": admission.error}), 409)
    job = db.session.get(ProcessingJob, admission.job_id)
    if not job:
        return None, (jsonify({"ok": False, "error": "processing job not found after admission"}), 500)
    return job, None


@bp.route("/api/interviews/<int:interview_id>/transcribe", methods=["POST"])
def start_transcription(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    project_id = int(interview.project_id)
    media = canonical_media_for_interview(interview.id)
    if media is None:
        return jsonify({"error": "音声ファイルが登録されていません"}), 400

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

    admission = admit_processing_job(project_id, "transcribe", interview_id)
    job, error_response = _resolve_admission(admission)
    if error_response:
        return error_response
    if not admission.created:
        return _queued_response(job, False)

    error, pid = _launch_or_fail(job)
    if error:
        return jsonify({"ok": False, "job_id": job.id, "error": error}), 500
    return _queued_response(job, True, pid)


@bp.route("/api/interviews/<int:interview_id>/map", methods=["POST"])
def start_mapping(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    project_id = int(interview.project_id)
    if interview.status == "pending" or not interview.segments:
        return jsonify({"error": "先に文字起こしを完了してください"}), 409

    admission = admit_processing_job(project_id, "map", interview_id)
    job, error_response = _resolve_admission(admission)
    if error_response:
        return error_response
    if not admission.created:
        return _queued_response(job, False)

    error, pid = _launch_or_fail(job)
    if error:
        return jsonify({"ok": False, "job_id": job.id, "error": error}), 500
    return _queued_response(job, True, pid)


@bp.route("/api/projects/<int:project_id>/process/all", methods=["POST"])
def process_all(project_id):
    Project.query.get_or_404(project_id)
    admission = admit_processing_job(project_id, "project_pipeline", None)
    job, error_response = _resolve_admission(admission)
    if error_response:
        return error_response
    if not admission.created:
        return _queued_response(job, False)

    error, pid = _launch_or_fail(job)
    if error:
        return jsonify({"ok": False, "job_id": job.id, "error": error}), 500
    return _queued_response(job, True, pid)


@bp.route("/api/projects/<int:project_id>/processing-status")
def get_project_processing_status(project_id):
    project = Project.query.get_or_404(project_id)
    recover_stale_jobs(project_id=project.id)
    latest_job = (
        ProcessingJob.query
        .filter_by(project_id=project.id, interview_id=None, job_type="project_pipeline")
        .order_by(ProcessingJob.id.desc())
        .first()
    )
    return jsonify({
        "ok": True,
        "project_id": project.id,
        "latest_job": latest_job.to_dict() if latest_job else None,
    })


@bp.route("/api/interviews/<int:interview_id>/status")
def get_status(interview_id):
    interview = Interview.query.get_or_404(interview_id)
    recover_stale_jobs(project_id=interview.project_id)

    tr = None
    media = canonical_media_for_interview(interview.id)
    if media is not None:
        tr = (
            Transcription.query
            .filter_by(media_file_id=media.id)
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
    ProcessingJob.query.get_or_404(job_id)
    admission = admit_retry_job(job_id)
    job, error_response = _resolve_admission(admission)
    if error_response:
        return error_response

    error, pid = _launch_or_fail(job)
    if error:
        return jsonify({"ok": False, "job_id": job.id, "error": error}), 500
    return _queued_response(job, True, pid)
