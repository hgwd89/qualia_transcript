from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy.exc import IntegrityError

from models import db
from models.project import Project
from models.interview import Interview
from models.analysis import AIAnalysis
from models.processing_job import ProcessingJob
from models.segment import UtteranceMapping
from models.interview_flow import InterviewFlow, InterviewFlowSection, InterviewFlowQuestion

bp = Blueprint("flows", __name__)


def _get_project_flow_or_404(project_id: int, flow_id: int) -> InterviewFlow:
    return (
        InterviewFlow.query
        .filter_by(id=flow_id, project_id=project_id)
        .first_or_404()
    )


def _get_flow_section_or_404(flow_id: int, section_id: int) -> InterviewFlowSection:
    return (
        InterviewFlowSection.query
        .filter_by(id=section_id, flow_id=flow_id)
        .first_or_404()
    )


def _question_code_conflict(flow_id: int, question_code: str) -> bool:
    return (
        InterviewFlowQuestion.query
        .join(
            InterviewFlowSection,
            InterviewFlowQuestion.section_id == InterviewFlowSection.id,
        )
        .filter(
            InterviewFlowSection.flow_id == int(flow_id),
            InterviewFlowQuestion.question_code == str(question_code),
        )
        .first()
        is not None
    )


def _duplicate_question_code_response(project: Project, flow: InterviewFlow):
    flash("同じインタビューフロー内で質問コードは重複できません", "error")
    return render_template("flows/detail.html", project=project, flow=flow), 409


def _flow_usage_counts(flow_id: int) -> dict[str, int]:
    mapping_count = (
        UtteranceMapping.query
        .join(InterviewFlowQuestion, UtteranceMapping.question_id == InterviewFlowQuestion.id)
        .join(InterviewFlowSection, InterviewFlowQuestion.section_id == InterviewFlowSection.id)
        .filter(InterviewFlowSection.flow_id == flow_id)
        .count()
    )
    analysis_count = (
        AIAnalysis.query
        .join(InterviewFlowQuestion, AIAnalysis.question_id == InterviewFlowQuestion.id)
        .join(InterviewFlowSection, InterviewFlowQuestion.section_id == InterviewFlowSection.id)
        .filter(InterviewFlowSection.flow_id == flow_id)
        .count()
    )
    processing_job_count = (
        ProcessingJob.query
        .join(InterviewFlowQuestion, ProcessingJob.question_id == InterviewFlowQuestion.id)
        .join(InterviewFlowSection, InterviewFlowQuestion.section_id == InterviewFlowSection.id)
        .filter(InterviewFlowSection.flow_id == flow_id)
        .count()
    )
    return {
        "interviews": Interview.query.filter_by(flow_id=flow_id).count(),
        "mappings": mapping_count,
        "analyses": analysis_count,
        "processing_jobs": processing_job_count,
    }


@bp.route("/projects/<int:project_id>/flows")
def index(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template("flows/index.html", project=project, flows=project.interview_flows)


@bp.route("/projects/<int:project_id>/flows/new", methods=["GET", "POST"])
def new(project_id):
    project = Project.query.get_or_404(project_id)
    if request.method == "POST":
        flow = InterviewFlow(
            project_id=project_id,
            title=request.form.get("title", "").strip(),
            version=request.form.get("version", "1.0").strip(),
            notes=request.form.get("notes", "").strip() or None,
        )
        db.session.add(flow)
        db.session.commit()
        flash(f"インタビューフロー「{flow.title}」を作成しました", "success")
        return redirect(url_for("flows.detail", project_id=project_id, flow_id=flow.id))
    return render_template("flows/new.html", project=project)


@bp.route("/projects/<int:project_id>/flows/<int:flow_id>")
def detail(project_id, flow_id):
    project = Project.query.get_or_404(project_id)
    flow = _get_project_flow_or_404(project_id, flow_id)
    return render_template("flows/detail.html", project=project, flow=flow)


@bp.route("/projects/<int:project_id>/flows/<int:flow_id>/sections/new", methods=["POST"])
def add_section(project_id, flow_id):
    Project.query.get_or_404(project_id)
    flow = _get_project_flow_or_404(project_id, flow_id)
    seq = len(flow.sections) + 1
    section = InterviewFlowSection(
        flow_id=flow.id,
        title=request.form.get("title", f"セクション {seq}").strip(),
        description=request.form.get("description", "").strip() or None,
        seq=seq,
    )
    db.session.add(section)
    db.session.commit()
    flash("セクションを追加しました", "success")
    return redirect(url_for("flows.detail", project_id=project_id, flow_id=flow.id))


@bp.route("/projects/<int:project_id>/flows/<int:flow_id>/sections/<int:section_id>/questions/new",
          methods=["POST"])
def add_question(project_id, flow_id, section_id):
    project = Project.query.get_or_404(project_id)
    flow = _get_project_flow_or_404(project_id, flow_id)
    section = _get_flow_section_or_404(flow.id, section_id)
    seq = len(section.questions) + 1
    question_code = request.form.get("question_code", "").strip() or f"Q{section.seq}-{seq}"
    if _question_code_conflict(flow.id, question_code):
        return _duplicate_question_code_response(project, flow)

    q = InterviewFlowQuestion(
        section_id=section.id,
        question_code=question_code,
        question_text=request.form.get("question_text", "").strip(),
        question_type=request.form.get("question_type", "open"),
        is_key_question=request.form.get("is_key_question") == "1",
        seq=seq,
    )
    try:
        db.session.add(q)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flow = _get_project_flow_or_404(project_id, flow_id)
        return _duplicate_question_code_response(project, flow)

    flash("質問項目を追加しました", "success")
    return redirect(url_for("flows.detail", project_id=project_id, flow_id=flow.id))


@bp.route("/projects/<int:project_id>/flows/<int:flow_id>/delete", methods=["POST"])
def delete(project_id, flow_id):
    Project.query.get_or_404(project_id)
    flow = _get_project_flow_or_404(project_id, flow_id)
    usage = _flow_usage_counts(flow.id)
    if any(usage.values()):
        flash(
            "このインタビューフローはインタビュー／マッピング／分析／処理ジョブで使用されているため削除できません",
            "error",
        )
        return redirect(url_for("flows.index", project_id=project_id))

    db.session.delete(flow)
    db.session.commit()
    flash("インタビューフローを削除しました", "info")
    return redirect(url_for("flows.index", project_id=project_id))