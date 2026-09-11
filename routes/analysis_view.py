"""
分析結果表示・プロジェクトレベル AI 分析トリガー・人手レビュー。
"""
import json
from datetime import datetime, timezone

from flask import Blueprint, render_template, jsonify, request

from models import db
from models.project import Project
from models.interview_flow import InterviewFlowQuestion
from models.analysis import AIAnalysis
from models.processing_job import ProcessingJob
from services.analysis_review import set_analysis_review_status
from services.job_admission import admit_processing_job
from services.job_recovery import recover_stale_jobs
from services.processing_jobs import launch_job_worker

bp = Blueprint("analysis_view", __name__)
_PROJECT_ANALYSIS_JOB_TYPES = {"analyze_cross", "analyze_integrated"}


def _parse(a: AIAnalysis) -> dict:
    content = {}
    if a.content_json:
        try:
            content = json.loads(a.content_json)
        except (json.JSONDecodeError, TypeError):
            pass
    return {"obj": a, "content": content}


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


def _queue_project_analysis(project_id: int, job_type: str, *, question_id: int | None = None):
    admission = admit_processing_job(
        project_id,
        job_type,
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


@bp.route("/projects/<int:project_id>/analysis/review")
def review_index(project_id):
    project = Project.query.get_or_404(project_id)
    analyses = [
        _parse(a)
        for a in (
            AIAnalysis.query
            .filter_by(project_id=project_id)
            .order_by(AIAnalysis.created_at.desc(), AIAnalysis.id.desc())
            .all()
        )
    ]
    status_counts = {"draft": 0, "approved": 0, "rejected": 0}
    for item in analyses:
        status = item["obj"].review_status or "draft"
        status_counts[status] = status_counts.get(status, 0) + 1

    return render_template(
        "analysis/review.html",
        project=project,
        analyses=analyses,
        status_counts=status_counts,
    )


@bp.route(
    "/api/projects/<int:project_id>/analysis/<int:analysis_id>/review",
    methods=["POST"],
)
def review_analysis(project_id, analysis_id):
    Project.query.get_or_404(project_id)
    analysis = AIAnalysis.query.filter_by(id=analysis_id, project_id=project_id).first()
    if not analysis:
        return jsonify({"error": "このプロジェクトに属するAI分析が見つかりません"}), 404

    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    note = payload.get("note")

    try:
        analysis, unresolved = set_analysis_review_status(analysis, status, note)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if unresolved:
        return jsonify({
            "error": "根拠発言を source_segment_ids に解決できない finding があるため承認できません",
            "unresolved": unresolved,
        }), 409

    return jsonify({
        "ok": True,
        "analysis_id": analysis.id,
        "review_status": analysis.review_status,
        "reviewed_at": analysis.reviewed_at.isoformat() if analysis.reviewed_at else None,
    })


@bp.route("/api/projects/<int:project_id>/analyze/cross/<int:question_id>", methods=["POST"])
def run_cross(project_id, question_id):
    Project.query.get_or_404(project_id)
    question = InterviewFlowQuestion.query.get_or_404(question_id)
    if not question.section or not question.section.flow or question.section.flow.project_id != project_id:
        return jsonify({"ok": False, "error": "質問がこのプロジェクトに属していません"}), 404
    return _queue_project_analysis(project_id, "analyze_cross", question_id=question_id)


@bp.route("/api/projects/<int:project_id>/analyze/integrated", methods=["POST"])
def run_integrated(project_id):
    Project.query.get_or_404(project_id)
    return _queue_project_analysis(project_id, "analyze_integrated")


@bp.route("/api/projects/<int:project_id>/analysis-processing-status")
def analysis_processing_status(project_id):
    Project.query.get_or_404(project_id)
    recover_stale_jobs(project_id=project_id)
    latest = (
        ProcessingJob.query
        .filter_by(project_id=project_id)
        .filter(ProcessingJob.job_type.in_(_PROJECT_ANALYSIS_JOB_TYPES))
        .order_by(ProcessingJob.id.desc())
        .first()
    )
    return jsonify({"ok": True, "latest_job": latest.to_dict() if latest else None})
