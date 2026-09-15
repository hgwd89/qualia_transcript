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
    formal_artifact_expected_sha256,
)
from services.file_manager import (
    generated_file_expected_sha256,
    generated_file_source_provenance,
)
from services.formal_artifact_integrity import (
    FormalArtifactIntegrityError,
    verified_artifact_snapshot,
)
from services.generated_file_source_provenance import (
    SUPPORTED_FILE_TYPES as SOURCE_BOUND_FILE_TYPES,
    source_provenance_matches_scope,
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


def _begin_serialized_download_snapshot() -> None:
    """Serialize source/currentness validation until verified bytes are snapshotted."""
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("generated artifact download requires a clean database session")
    db.session.rollback()
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))


def _ordinary_source_status(generated_file: GeneratedFile) -> tuple[bool | None, str]:
    if generated_file.file_type not in SOURCE_BOUND_FILE_TYPES:
        return None, ""
    try:
        provenance = generated_file_source_provenance(generated_file)
    except ValueError as exc:
        return False, str(exc)
    if provenance is None:
        return None, "legacy artifact has no source provenance"
    return source_provenance_matches_scope(
        provenance,
        str(generated_file.file_type),
        int(generated_file.project_id),
        interview_id=(
            int(generated_file.interview_id)
            if generated_file.interview_id is not None
            else None
        ),
    )


def _output_file_item(generated_file: GeneratedFile) -> dict:
    status = approved_analysis_artifact_currentness(generated_file)
    ordinary_current, ordinary_reason = _ordinary_source_status(generated_file)
    return {
        "obj": generated_file,
        "formal_current": status.current,
        "formal_reason": status.reason,
        "ordinary_current": ordinary_current,
        "ordinary_reason": ordinary_reason,
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
            _begin_serialized_download_snapshot()
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

            expected_artifact_sha256 = formal_artifact_expected_sha256(gf)
            stored_path = str(gf.stored_path)
            download_name = gf.original_filename or Path(stored_path).name
            opened = open_managed_file_for_read(config.OUTPUT_DIR, gf.stored_path)
            opened = verified_artifact_snapshot(opened, expected_artifact_sha256)
            db.session.commit()
        except FormalArtifactIntegrityError as exc:
            db.session.rollback()
            if opened is not None:
                opened.close()
            abort(409, description=f"承認済AI分析ファイルのbytes整合性を検証できません: {exc}")
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
            expected_artifact_sha256 = generated_file_expected_sha256(gf)
            source_provenance = generated_file_source_provenance(gf)
        except ValueError as exc:
            abort(409, description=f"生成済みファイルの整合性メタデータを検証できません: {exc}")

        if source_provenance is not None:
            try:
                _begin_serialized_download_snapshot()
                db.session.expire_all()
                gf = db.session.get(GeneratedFile, int(file_id))
                if gf is None:
                    db.session.rollback()
                    abort(404)
                source_provenance = generated_file_source_provenance(gf)
                if source_provenance is None:
                    db.session.rollback()
                    abort(409, description="生成済みファイルのsource provenanceが失われています")
                current, reason = source_provenance_matches_scope(
                    source_provenance,
                    str(gf.file_type),
                    int(gf.project_id),
                    interview_id=(
                        int(gf.interview_id) if gf.interview_id is not None else None
                    ),
                )
                if not current:
                    db.session.rollback()
                    abort(409, description=(
                        "この生成済みファイルは現在のcanonical dataに対する出力として検証できません: "
                        + reason
                    ))
                expected_artifact_sha256 = generated_file_expected_sha256(gf)
                if expected_artifact_sha256 is None:
                    db.session.rollback()
                    abort(409, description="source-bound generated file has no artifact SHA-256")
                stored_path = str(gf.stored_path)
                download_name = gf.original_filename or Path(stored_path).name
                opened = open_managed_file_for_read(config.OUTPUT_DIR, gf.stored_path)
                opened = verified_artifact_snapshot(opened, expected_artifact_sha256)
                db.session.commit()
            except FormalArtifactIntegrityError as exc:
                db.session.rollback()
                if opened is not None:
                    opened.close()
                abort(409, description=f"生成済みファイルのbytes整合性を検証できません: {exc}")
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
                opened = open_managed_file_for_read(config.OUTPUT_DIR, gf.stored_path)
            except (OSError, ValueError):
                abort(404)

            if expected_artifact_sha256 is not None:
                try:
                    opened = verified_artifact_snapshot(opened, expected_artifact_sha256)
                except FormalArtifactIntegrityError as exc:
                    if opened is not None:
                        opened.close()
                    abort(409, description=f"生成済みファイルのbytes整合性を検証できません: {exc}")

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
        response.content_length = int(getattr(opened, "size", info.st_size))
        response.set_etag(_managed_download_etag(info))
        response.make_conditional(
            request,
            accept_ranges=True,
            complete_length=int(getattr(opened, "size", info.st_size)),
        )
    except Exception:
        opened.close()
        raise

    response.call_on_close(opened.close)
    return response
