from pathlib import Path

from flask import Blueprint, jsonify, request, send_file, abort, render_template
from sqlalchemy import text

import config
from models import db
from models.project import Project
from models.interview import Interview
from models.analysis import AIAnalysis
from models.generated_file import GeneratedFile
from services.approved_analysis_currentness import (
    approved_analysis_artifact_currentness,
    formal_approved_analysis_readiness,
)
from services.report_verbatim import generate_verbatim
from services.report_formatted import generate_formatted_sheet
from services.report_analysis import generate_analysis_xlsx, generate_analysis_csv
from services.report_approved_analysis import generate_approved_analysis_xlsx
from services.storage_paths import open_managed_file_for_read

bp = Blueprint("outputs", __name__)


def _managed_download_etag(info) -> str:
    values = (
        getattr(info, "st_dev", 0) or 0,
        getattr(info, "st_ino", 0) or 0,
        getattr(info, "st_size", 0) or 0,
        getattr(info, "st_mtime_ns", 0) or 0,
    )
    return "-".join(f"{int(value):x}" for value in values)


def _begin_formal_download_snapshot() -> None:
    """Serialize formal-artifact validation until its exact bytes are pinned."""
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("formal artifact download requires a clean database session")
    db.session.rollback()
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))


def _output_file_item(generated_file: GeneratedFile) -> dict:
    status = approved_analysis_artifact_currentness(generated_file)
    return {
        "obj": generated_file,
        "formal_current": status.current,
        "formal_reason": status.reason,
    }


@bp.route("/projects/<int:project_id>/outputs")
def index(project_id):
    project = Project.query.get_or_404(project_id)
    files = (
        GeneratedFile.query
        .filter_by(project_id=project_id)
        .order_by(GeneratedFile.created_at.desc())
        .all()
    )
    formal_readiness = formal_approved_analysis_readiness(project_id)
    analysis_count = AIAnalysis.query.filter_by(project_id=project_id).count()
    return render_template(
        "outputs/index.html",
        project=project,
        files=[_output_file_item(item) for item in files],
        approved_count=formal_readiness["approved_count"],
        current_approved_count=formal_readiness["current_count"],
        invalid_approved_count=formal_readiness["invalid_count"],
        formal_ready=formal_readiness["ready"],
        analysis_count=analysis_count,
    )


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


@bp.route("/api/projects/<int:project_id>/generate/approved-analysis", methods=["POST"])
def gen_approved_analysis(project_id):
    Project.query.get_or_404(project_id)
    try:
        gf = generate_approved_analysis_xlsx(project_id)
        return jsonify({"ok": True, "file_id": gf.id, "filename": gf.original_filename})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/outputs/<int:file_id>/download")
def download(file_id):
    gf = GeneratedFile.query.get_or_404(file_id)
    opened = None
    stored_path = str(gf.stored_path)
    download_name = gf.original_filename or Path(stored_path).name

    if gf.file_type == "approved_analysis":
        try:
            _begin_formal_download_snapshot()
            db.session.expire_all()
            gf = db.session.get(GeneratedFile, int(file_id))
            if gf is None:
                db.session.rollback()
                abort(404)

            currentness = approved_analysis_artifact_currentness(gf)
            if not currentness.current:
                db.session.rollback()
                abort(409, description=(
                    "この承認済AI分析ファイルは現在のcanonical dataに対する正式出力として検証できません: "
                    + currentness.reason
                ))

            stored_path = str(gf.stored_path)
            download_name = gf.original_filename or Path(stored_path).name
            opened = open_managed_file_for_read(config.OUTPUT_DIR, stored_path)
            # Retain both the exact managed-file handle and its download metadata
            # before releasing the serialized DB snapshot. No ORM reload is
            # needed after the write reservation is released.
            db.session.commit()
        except (OSError, ValueError):
            db.session.rollback()
            if opened is not None:
                opened.close()
            abort(404)
        except Exception:
            db.session.rollback()
            if opened is not None:
                opened.close()
            raise
    else:
        try:
            opened = open_managed_file_for_read(config.OUTPUT_DIR, stored_path)
        except (OSError, ValueError):
            abort(404)

    info = opened.stat_result
    try:
        response = send_file(
            opened.stream,
            as_attachment=True,
            download_name=download_name,
            conditional=False,
            etag=False,
            last_modified=float(info.st_mtime),
        )
        response.content_length = int(info.st_size)
        response.set_etag(_managed_download_etag(info))
        response.make_conditional(
            request,
            accept_ranges=True,
            complete_length=int(info.st_size),
        )
    except Exception:
        opened.close()
        raise

    response.call_on_close(opened.close)
    return response
