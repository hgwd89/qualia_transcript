from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from datetime import datetime, timezone
from models import db
from models.project import Project

bp = Blueprint("projects", __name__)

STATUS_LABELS = {
    "draft":       "準備中",
    "in_progress": "進行中",
    "completed":   "完了",
}


@bp.route("/")
def index():
    projects = Project.query.order_by(Project.created_at.desc()).all()
    return render_template("projects/index.html", projects=projects, status_labels=STATUS_LABELS)


@bp.route("/projects/new", methods=["GET", "POST"])
def new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("プロジェクト名は必須です", "error")
            return render_template("projects/new.html", status_labels=STATUS_LABELS)
        project = Project(
            name=name,
            client=request.form.get("client", "").strip() or None,
            research_objective=request.form.get("research_objective", "").strip() or None,
            description=request.form.get("description", "").strip() or None,
            method=request.form.get("method", "DI"),
            status=request.form.get("status", "draft"),
        )
        db.session.add(project)
        db.session.commit()
        flash(f"プロジェクト「{project.name}」を作成しました", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template("projects/new.html", status_labels=STATUS_LABELS)


@bp.route("/projects/<int:project_id>")
def detail(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template("projects/detail.html", project=project, status_labels=STATUS_LABELS)


@bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
def edit(project_id):
    project = Project.query.get_or_404(project_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("プロジェクト名は必須です", "error")
            return render_template("projects/edit.html", project=project, status_labels=STATUS_LABELS)
        project.name               = name
        project.client             = request.form.get("client", "").strip() or None
        project.research_objective = request.form.get("research_objective", "").strip() or None
        project.description        = request.form.get("description", "").strip() or None
        project.method             = request.form.get("method", project.method or "DI")
        project.status             = request.form.get("status", project.status or "draft")
        project.updated_at         = datetime.now(timezone.utc)
        db.session.commit()
        flash("プロジェクトを更新しました", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template("projects/edit.html", project=project, status_labels=STATUS_LABELS)


@bp.route("/projects/<int:project_id>/delete", methods=["POST"])
def delete(project_id):
    project = Project.query.get_or_404(project_id)
    name = project.name
    db.session.delete(project)
    db.session.commit()
    flash(f"プロジェクト「{name}」を削除しました", "info")
    return redirect(url_for("projects.index"))
