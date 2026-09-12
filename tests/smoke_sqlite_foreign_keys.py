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
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_fk_") as tmp:
        root = Path(tmp)
        db_path = root / "fk.db"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        try:
            from sqlalchemy import text
            from sqlalchemy.exc import IntegrityError
            from app import create_app
            from models import db
            from models.project import Project
            from models.participant import Participant
            from models.processing_job import ProcessingJob

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                pragma_value = db.session.execute(text("PRAGMA foreign_keys")).scalar()
                failures += check(
                    "SQLite foreign key pragma is enabled",
                    int(pragma_value or 0) == 1,
                    f"foreign_keys={pragma_value}",
                )

                project = Project(name="Valid parent")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                valid = Participant(
                    project_id=project_id,
                    participant_code="P01",
                )
                db.session.add(valid)
                db.session.commit()
                failures += check(
                    "valid foreign key insert succeeds",
                    valid.id is not None and valid.project_id == project_id,
                )

                rejected = False
                db.session.add(Participant(
                    project_id=project_id + 999999,
                    participant_code="BAD",
                ))
                try:
                    db.session.commit()
                except IntegrityError:
                    rejected = True
                    db.session.rollback()
                failures += check(
                    "invalid foreign key insert is rejected",
                    rejected,
                )

                baseline_job = ProcessingJob(
                    project_id=project_id,
                    question_id=None,
                    job_type="analyze_question",
                    status="succeeded",
                )
                db.session.add(baseline_job)
                db.session.commit()
                baseline_job_id = int(baseline_job.id)

                trigger_names = {
                    str(row[0])
                    for row in db.session.execute(text(
                        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='processing_jobs'"
                    )).all()
                }
                failures += check(
                    "processing-job question compatibility triggers are installed",
                    {
                        "trg_processing_jobs_question_insert",
                        "trg_processing_jobs_question_update",
                    }.issubset(trigger_names),
                    str(trigger_names),
                )

                db.session.remove()
                db.engine.dispose()

            # Prove the compatibility guard works independently of PRAGMA FK
            # enforcement, matching the legacy-table case that lacks question_id FK.
            raw = sqlite3.connect(str(db_path))
            try:
                raw.execute("PRAGMA foreign_keys=OFF")
                insert_rejected = False
                try:
                    raw.execute(
                        """
                        INSERT INTO processing_jobs(
                            project_id, interview_id, question_id, job_type, status,
                            attempt_count, created_at
                        ) VALUES (?, NULL, ?, 'analyze_question', 'pending', 0, ?)
                        """,
                        (project_id, 999999999, "2026-09-12 00:00:00"),
                    )
                    raw.commit()
                except sqlite3.IntegrityError:
                    insert_rejected = True
                    raw.rollback()
                failures += check(
                    "question trigger rejects orphan insert with FK pragma disabled",
                    insert_rejected,
                )

                update_rejected = False
                try:
                    raw.execute(
                        "UPDATE processing_jobs SET question_id=? WHERE id=?",
                        (888888888, baseline_job_id),
                    )
                    raw.commit()
                except sqlite3.IntegrityError:
                    update_rejected = True
                    raw.rollback()
                failures += check(
                    "question trigger rejects orphan update with FK pragma disabled",
                    update_rejected,
                )
            finally:
                raw.close()
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
