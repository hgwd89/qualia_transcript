import os
from flask import Blueprint, jsonify, request, send_file, abort, render_template
import config
from models.project import Project
from models.interview import Interview
from models.generated_file import GeneratedFile
from services.report_verbatim import generate_verbatim
from services.report_formatted import generate_formatted_sheet
from services.report_analysis import generate_analysis_xlsx, generate_analysis_csv

bp = Blueprint("outputs", __name__)


@bp.route("/projects/<int:project_id>/outputs")
def index(project_id):
    project = Project.query.get_or_404(project_id)
    files = (
        GeneratedFile.query
        .filter_by(project_id=project_id)
        .order_by(GeneratedFile.created_at.desc())
        .all()
    )
    return render_template("outputs/index.html", project=project, files=files)


@bp.route("/api/projects/<int:project_id>/generate/verbatim", methods=["POST"])
def gen_verbatim(project_id):
    project = Project.query.get_or_404(project_id)
    interview_id = request.json.get("interview_id") if request.is_json else None
    if not interview_id:
        return jsonify({"error": "interview_id が必要です"}), 400

    try:
        interview_id = int(interview_id)
    except (TypeError, ValueError):
        return jsonify({"error": "interview_id が不正です"}), 400

    interview = Interview.query.filter_by(id=interview_id, project_id=project.id).first()
    if not interview:
        return jsonify({"error": "このプロジェクトに属する interview が見つかりません"}), 404

    try:
        gf = generate_verbatim(interview.id)
        return jsonify({"ok": True, "file_id": gf.id, "filename": gf.original_filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/projects/<int:project_id>/generate/formatted", methods=["POST"])
def gen_formatted(project_id):
    Project.query.get_or_404(project_id)
    try:
        gf = generate_formatted_sheet(project_id)
        return jsonify({"ok": True, "file_id": gf.id, "filename": gf.original_filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/projects/<int:project_id>/generate/analysis", methods=["POST"])
def gen_analysis(project_id):
    Project.query.get_or_404(project_id)
    payload = request.get_json(silent=True) or {}
    fmt = str(payload.get("format", "xlsx")).lower()
    if fmt not in {"xlsx", "csv"}:
        return jsonify({"error": "format は xlsx または csv を指定してください"}), 400

    try:
        gf = generate_analysis_csv(project_id) if fmt == "csv" else generate_analysis_xlsx(project_id)
        return jsonify({"ok": True, "file_id": gf.id, "filename": gf.original_filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/outputs/<int:file_id>/download")
def download(file_id):
    gf = GeneratedFile.query.get_or_404(file_id)
    full_path = os.path.join(config.OUTPUT_DIR, gf.stored_path)
    if not os.path.isfile(full_path):
        abort(404)
    return send_file(
        full_path,
        as_attachment=True,
        download_name=gf.original_filename,
    )
