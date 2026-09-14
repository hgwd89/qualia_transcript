import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def has_code(items: list[dict], code: str, file_id: int | None = None) -> bool:
    for item in items:
        if item.get("code") != code:
            continue
        if file_id is None:
            return True
        context = item.get("context") or {}
        if int(context.get("generated_file_id") or 0) == int(file_id):
            return True
    return False


def write_target(target, payload: bytes, open_output_target_for_write) -> None:
    opened = open_output_target_for_write(target)
    try:
        opened.stream.write(payload)
    finally:
        opened.close()


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "scripts"
    for entry in (repo_root, scripts_dir):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }
    failures = 0

    with tempfile.TemporaryDirectory(prefix="qualia_generated_owner_") as tmp:
        root = Path(tmp)
        db_path = root / "ownership.db"
        output_dir = root / "outputs"
        upload_dir = root / "uploads"
        backup_dir = root / "backups"
        output_dir.mkdir()
        upload_dir.mkdir()
        backup_dir.mkdir()
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(output_dir)
        config.UPLOAD_DIR = str(upload_dir)
        config.BACKUP_DIR = str(backup_dir)

        try:
            from app import create_app
            from audit_production_readiness_final import audit_final
            from models import db
            from models.generated_file import GeneratedFile
            from models.interview import Interview
            from models.project import Project
            from services.file_manager import (
                open_output_target_for_write,
                prepare_output_target,
                register_generated_file,
            )
            from services.generated_file_ownership import inspect_generated_file_ownership

            app = create_app()
            with app.app_context():
                project_a = Project(name="Ownership A")
                project_b = Project(name="Ownership B")
                db.session.add_all([project_a, project_b])
                db.session.commit()
                project_a_id = int(project_a.id)
                project_b_id = int(project_b.id)

                interview_b = Interview(project_id=project_b_id)
                db.session.add(interview_b)
                db.session.commit()
                interview_b_id = int(interview_b.id)

                valid_target = prepare_output_target(project_a_id, "valid.csv")
                write_target(valid_target, b"a,b\r\n1,2\r\n", open_output_target_for_write)
                valid = register_generated_file(
                    valid_target,
                    project_id=project_a_id,
                    file_type="analysis",
                    file_format="csv",
                )
                valid_id = int(valid.id)
                failures += check(
                    "registrar accepts a project-owned output namespace",
                    valid.project_id == project_a_id
                    and valid.interview_id is None
                    and str(valid.stored_path).startswith(f"{project_a_id}/"),
                    str(valid.stored_path),
                )

                cross_interview_target = prepare_output_target(project_a_id, "cross-interview.csv")
                write_target(
                    cross_interview_target,
                    b"x,y\r\n3,4\r\n",
                    open_output_target_for_write,
                )
                cross_interview_path = Path(cross_interview_target.full_path)
                before_count = GeneratedFile.query.count()
                rejected = False
                try:
                    register_generated_file(
                        cross_interview_target,
                        project_id=project_a_id,
                        interview_id=interview_b_id,
                        file_type="analysis",
                        file_format="csv",
                    )
                except ValueError as exc:
                    rejected = "another project" in str(exc)
                failures += check(
                    "registrar rejects an interview owned by another project and removes rejected bytes",
                    rejected
                    and GeneratedFile.query.count() == before_count
                    and not cross_interview_path.exists(),
                    f"rejected={rejected} count={GeneratedFile.query.count()} path_exists={cross_interview_path.exists()}",
                )

                cross_path_target = prepare_output_target(project_b_id, "cross-path.csv")
                write_target(cross_path_target, b"m,n\r\n5,6\r\n", open_output_target_for_write)
                cross_path = Path(cross_path_target.full_path)
                rejected = False
                try:
                    register_generated_file(
                        cross_path_target,
                        project_id=project_a_id,
                        file_type="analysis",
                        file_format="csv",
                    )
                except ValueError as exc:
                    rejected = "namespace" in str(exc)
                failures += check(
                    "registrar rejects another project's output directory and removes rejected bytes",
                    rejected and not cross_path.exists(),
                    f"rejected={rejected} path_exists={cross_path.exists()}",
                )

                dirty_target = prepare_output_target(project_a_id, "dirty-session.csv")
                write_target(dirty_target, b"p,q\r\n7,8\r\n", open_output_target_for_write)
                dirty_path = Path(dirty_target.full_path)
                unsaved = Project(name="must not commit")
                db.session.add(unsaved)
                rejected = False
                try:
                    register_generated_file(
                        dirty_target,
                        project_id=project_a_id,
                        file_type="analysis",
                        file_format="csv",
                    )
                except RuntimeError as exc:
                    rejected = "clean database session" in str(exc)
                failures += check(
                    "registrar refuses unrelated pending DB writes and does not publish them",
                    rejected
                    and unsaved.id is None
                    and not dirty_path.exists()
                    and Project.query.filter_by(name="must not commit").count() == 0,
                    f"rejected={rejected} unsaved_id={unsaved.id} path_exists={dirty_path.exists()}",
                )

                cross_interview_row = GeneratedFile(
                    project_id=project_a_id,
                    interview_id=interview_b_id,
                    file_type="analysis",
                    file_format="csv",
                    original_filename="cross-interview-existing.csv",
                    stored_path=f"{project_a_id}/existing-cross-interview.csv",
                )
                cross_path_row = GeneratedFile(
                    project_id=project_a_id,
                    file_type="analysis",
                    file_format="csv",
                    original_filename="cross-path-existing.csv",
                    stored_path=f"{project_b_id}/existing-cross-path.csv",
                )
                traversed_path_row = GeneratedFile(
                    project_id=project_a_id,
                    file_type="analysis",
                    file_format="csv",
                    original_filename="traversed-existing.csv",
                    stored_path=f"{project_a_id}/../{project_b_id}/traversed-existing.csv",
                )
                legacy_row = GeneratedFile(
                    project_id=project_a_id,
                    file_type="analysis",
                    file_format="csv",
                    original_filename="legacy.csv",
                    stored_path="legacy.csv",
                )
                db.session.add_all([
                    cross_interview_row,
                    cross_path_row,
                    traversed_path_row,
                    legacy_row,
                ])
                db.session.commit()
                cross_interview_id = int(cross_interview_row.id)
                cross_path_id = int(cross_path_row.id)
                traversed_path_id = int(traversed_path_row.id)
                legacy_id = int(legacy_row.id)
                db.session.remove()
                db.engine.dispose()

            con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            try:
                ownership = inspect_generated_file_ownership(con)
                project_ownership = inspect_generated_file_ownership(
                    con,
                    project_id=project_a_id,
                )
            finally:
                con.close()

            failures += check(
                "readiness ownership scan blocks cross-project interview metadata",
                has_code(
                    ownership.blockers,
                    "generated_file_interview_project_mismatch",
                    cross_interview_id,
                ),
                str(ownership.blockers),
            )
            failures += check(
                "readiness ownership scan blocks another project's stored_path namespace",
                has_code(
                    ownership.blockers,
                    "generated_file_project_path_mismatch",
                    cross_path_id,
                )
                and has_code(
                    ownership.blockers,
                    "generated_file_project_path_mismatch",
                    traversed_path_id,
                ),
                str(ownership.blockers),
            )
            failures += check(
                "legacy root-level generated paths remain warning-only",
                has_code(
                    ownership.warnings,
                    "generated_file_project_path_unscoped",
                    legacy_id,
                )
                and not any(
                    int((item.get("context") or {}).get("generated_file_id") or 0) == legacy_id
                    for item in ownership.blockers
                ),
                f"warnings={ownership.warnings} blockers={ownership.blockers}",
            )
            failures += check(
                "project-scoped ownership scan still sees selected project's cross-project targets",
                has_code(
                    project_ownership.blockers,
                    "generated_file_interview_project_mismatch",
                    cross_interview_id,
                )
                and has_code(
                    project_ownership.blockers,
                    "generated_file_project_path_mismatch",
                    cross_path_id,
                ),
                str(project_ownership.blockers),
            )

            final_report = audit_final(
                db_path,
                output_dir,
                backup_dir,
                upload_dir,
                project_id=project_a_id,
            )
            failures += check(
                "final release readiness includes generated-file ownership blockers",
                has_code(
                    final_report.get("blockers", []),
                    "generated_file_interview_project_mismatch",
                    cross_interview_id,
                )
                and has_code(
                    final_report.get("blockers", []),
                    "generated_file_project_path_mismatch",
                    cross_path_id,
                )
                and int(
                    final_report.get("info", {}).get(
                        "generated_file_ownership_checked_count", 0
                    )
                ) >= 4,
                f"blockers={final_report.get('blockers', [])} info={final_report.get('info', {})}",
            )

            failures += check(
                "valid registered output is not falsely flagged by ownership scan",
                not any(
                    int((item.get("context") or {}).get("generated_file_id") or 0) == valid_id
                    for item in ownership.blockers + ownership.warnings
                ),
                f"blockers={ownership.blockers} warnings={ownership.warnings}",
            )
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            if original["BACKUP_DIR"] is not None:
                config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
