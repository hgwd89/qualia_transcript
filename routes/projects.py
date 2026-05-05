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

RESEARCH_CATEGORY_OPTIONS = [
    "skincare",
    "food",
    "appliance",
    "automotive",
    "finance",
    "healthcare",
    "daily_goods",
    "general",
]
RESEARCH_CATEGORY_LABELS = {
    "skincare": "スキンケア",
    "food": "食品",
    "appliance": "家電",
    "automotive": "自動車",
    "finance": "金融",
    "healthcare": "ヘルスケア",
    "daily_goods": "日用品",
    "general": "一般",
}
GLOSSARY_PROFILE_OPTIONS = ["skincare", "general"]
DELIVERABLE_TYPE_OPTIONS = [
    ("verbatim_and_sheet", "発言録 + 整形シート"),
    ("verbatim_only", "発言録のみ"),
    ("sheet_only", "整形シートのみ"),
    ("analysis_report", "分析レポート中心"),
]
CONFIDENTIALITY_LEVEL_OPTIONS = [
    ("standard", "標準"),
    ("internal", "社内限定"),
    ("high", "高機密"),
]


def _sanitize_category(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in RESEARCH_CATEGORY_OPTIONS else "general"


def _sanitize_profile(value: str | None) -> str:
    v = (value or "").strip().lower()
    return v if v in GLOSSARY_PROFILE_OPTIONS else "general"


def _sanitize_deliverable(value: str | None) -> str:
    allowed = {k for k, _ in DELIVERABLE_TYPE_OPTIONS}
    v = (value or "").strip()
    return v if v in allowed else "verbatim_and_sheet"


def _sanitize_confidentiality(value: str | None) -> str:
    allowed = {k for k, _ in CONFIDENTIALITY_LEVEL_OPTIONS}
    v = (value or "").strip()
    return v if v in allowed else "standard"


def _project_form_context():
    return {
        "status_labels": STATUS_LABELS,
        "research_category_options": RESEARCH_CATEGORY_OPTIONS,
        "research_category_labels": RESEARCH_CATEGORY_LABELS,
        "glossary_profile_options": GLOSSARY_PROFILE_OPTIONS,
        "deliverable_type_options": DELIVERABLE_TYPE_OPTIONS,
        "confidentiality_level_options": CONFIDENTIALITY_LEVEL_OPTIONS,
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
            return render_template("projects/new.html", **_project_form_context())
        project = Project(
            name=name,
            client=request.form.get("client", "").strip() or None,
            research_theme=request.form.get("research_theme", "").strip() or None,
            research_category=_sanitize_category(request.form.get("research_category")),
            glossary_profile=_sanitize_profile(request.form.get("glossary_profile")),
            research_objective=request.form.get("research_objective", "").strip() or None,
            description=request.form.get("description", "").strip() or None,
            method=request.form.get("method", "DI"),
            deliverable_type=_sanitize_deliverable(request.form.get("deliverable_type")),
            confidentiality_level=_sanitize_confidentiality(request.form.get("confidentiality_level")),
            status=request.form.get("status", "draft"),
        )
        db.session.add(project)
        db.session.commit()
        flash(f"プロジェクト「{project.name}」を作成しました", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template("projects/new.html", **_project_form_context())


@bp.route("/projects/<int:project_id>")
def detail(project_id):
    project = Project.query.get_or_404(project_id)
    return render_template("projects/detail.html", project=project, **_project_form_context())


@bp.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
def edit(project_id):
    project = Project.query.get_or_404(project_id)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("プロジェクト名は必須です", "error")
            return render_template("projects/edit.html", project=project, **_project_form_context())
        project.name               = name
        project.client             = request.form.get("client", "").strip() or None
        project.research_theme     = request.form.get("research_theme", "").strip() or None
        project.research_category  = _sanitize_category(request.form.get("research_category"))
        project.glossary_profile   = _sanitize_profile(request.form.get("glossary_profile"))
        project.research_objective = request.form.get("research_objective", "").strip() or None
        project.description        = request.form.get("description", "").strip() or None
        project.method             = request.form.get("method", project.method or "DI")
        project.deliverable_type   = _sanitize_deliverable(request.form.get("deliverable_type"))
        project.confidentiality_level = _sanitize_confidentiality(request.form.get("confidentiality_level"))
        project.status             = request.form.get("status", project.status or "draft")
        project.updated_at         = datetime.now(timezone.utc)
        db.session.commit()
        flash("プロジェクトを更新しました", "success")
        return redirect(url_for("projects.detail", project_id=project.id))
    return render_template("projects/edit.html", project=project, **_project_form_context())


@bp.route("/projects/<int:project_id>/delete", methods=["POST"])
def delete(project_id):
    project = Project.query.get_or_404(project_id)
    name = project.name
    db.session.delete(project)
    db.session.commit()
    flash(f"プロジェクト「{name}」を削除しました", "info")
    return redirect(url_for("projects.index"))
