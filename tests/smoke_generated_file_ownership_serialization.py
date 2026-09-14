import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "BACKUP_DIR": getattr(config, "BACKUP_DIR", None),
    }
    failures = 0

    with tempfile.TemporaryDirectory(prefix="qualia_generated_owner_serial_") as tmp:
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
            from models import db
            from models.interview import Interview
            from models.project import Project
            import services.file_manager as file_manager

            app = create_app()
            with app.app_context():
                project_a = Project(name="A")
                project_b = Project(name="B")
                db.session.add_all([project_a, project_b])
                db.session.commit()
                project_a_id = int(project_a.id)
                project_b_id = int(project_b.id)

                interview = Interview(project_id=project_a_id)
                db.session.add(interview)
                db.session.commit()
                interview_id = int(interview.id)

                target = file_manager.prepare_output_target(project_a_id, "serialized.csv")
                opened = file_manager.open_output_target_for_write(target)
                try:
                    opened.stream.write(b"a,b\r\n1,2\r\n")
                finally:
                    opened.close()

                original_validate = file_manager._validate_generated_file_ownership
                writer_blocked = {"value": False, "error": None}

                def validating_with_competing_writer(*args, **kwargs):
                    con = sqlite3.connect(db_path, timeout=0.05)
                    try:
                        try:
                            con.execute(
                                "UPDATE interviews SET project_id=? WHERE id=?",
                                (project_b_id, interview_id),
                            )
                            con.commit()
                        except sqlite3.OperationalError as exc:
                            writer_blocked["value"] = "locked" in str(exc).lower()
                            writer_blocked["error"] = str(exc)
                            con.rollback()
                    finally:
                        con.close()
                    return original_validate(*args, **kwargs)

                file_manager._validate_generated_file_ownership = validating_with_competing_writer
                try:
                    generated = file_manager.register_generated_file(
                        target,
                        project_id=project_a_id,
                        interview_id=interview_id,
                        file_type="verbatim",
                        file_format="csv",
                    )
                finally:
                    file_manager._validate_generated_file_ownership = original_validate

                db.session.expire_all()
                current_interview = db.session.get(Interview, interview_id)
                failures += check(
                    "registration write reservation blocks concurrent ownership mutation",
                    writer_blocked["value"],
                    str(writer_blocked),
                )
                failures += check(
                    "registered file and interview remain in the same project after commit",
                    int(generated.project_id) == project_a_id
                    and int(generated.interview_id) == interview_id
                    and int(current_interview.project_id) == project_a_id,
                    f"generated_project={generated.project_id} interview_project={current_interview.project_id}",
                )
                db.session.remove()
                db.engine.dispose()
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
