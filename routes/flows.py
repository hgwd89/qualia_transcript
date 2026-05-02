from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from models import db
from models.project import Project
from models.interview_flow import InterviewFlow, InterviewFlowSection, InterviewFlowQuestion

bp = Blueprint("flows", __name__)


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
    flow    = InterviewFlow.query.get_or_404(flow_id)
    return render_template("flows/detail.html", project=project, flow=flow)


@bp.route("/projects/<int:project_id>/flows/<int:flow_id>/sections/new", methods=["POST"])
def add_section(project_id, flow_id):
    flow = InterviewFlow.query.get_or_404(flow_id)
    seq  = len(flow.sections) + 1
    section = InterviewFlowSection(
        flow_id=flow_id,
        title=request.form.get("title", f"セクション {seq}").strip(),
        description=request.form.get("description", "").strip() or None,
        seq=seq,
    )
    db.session.add(section)
    db.session.commit()
    flash("セクションを追加しました", "success")
    return redirect(url_for("flows.detail", project_id=project_id, flow_id=flow_id))


@bp.route("/projects/<int:project_id>/flows/<int:flow_id>/sections/<int:section_id>/questions/new",
          methods=["POST"])
def add_question(project_id, flow_id, section_id):
    section = InterviewFlowSection.query.get_or_404(section_id)
    seq     = len(section.questions) + 1
    q = InterviewFlowQuestion(
        section_id=section_id,
        question_code=request.form.get("question_code", "").strip() or f"Q{section.seq}-{seq}",
        question_text=request.form.get("question_text", "").strip(),
        question_type=request.form.get("question_type", "open"),
        is_key_question=request.form.get("is_key_question") == "1",
        seq=seq,
    )
    db.session.add(q)
    db.session.commit()
    flash("質問項目を追加しました", "success")
    return redirect(url_for("flows.detail", project_id=project_id, flow_id=flow_id))


@bp.route("/projects/<int:project_id>/flows/<int:flow_id>/delete", methods=["POST"])
def delete(project_id, flow_id):
    flow = InterviewFlow.query.get_or_404(flow_id)
    db.session.delete(flow)
    db.session.commit()
    flash("インタビューフローを削除しました", "info")
    return redirect(url_for("flows.index", project_id=project_id))
