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

                preserved_job = ProcessingJob(
                    project_id=project_id,
                    job_type="analyze",
                    status="succeeded",
                )
                db.session.add(preserved_job)
                db.session.commit()
                preserved_job_id = int(preserved_job.id)

                db.session.remove()
                db.engine.dispose()

            # Simulate an installation that received question_id through the old
            # ALTER TABLE path: the column exists, but it has no FK constraint.
            raw = sqlite3.connect(str(db_path))
            try:
                raw.execute("PRAGMA foreign_keys=OFF")
                raw.execute("BEGIN IMMEDIATE")
                raw.execute("DROP TABLE IF EXISTS processing_jobs_legacy")
                raw.execute(
                    """
                    CREATE TABLE processing_jobs_legacy (
                        id INTEGER NOT NULL,
                        project_id INTEGER NOT NULL,
                        interview_id INTEGER,
                        question_id INTEGER,
                        job_type VARCHAR(30) NOT NULL,
                        status VARCHAR(20) NOT NULL,
                        progress_json TEXT,
                        result_json TEXT,
                        error_message TEXT,
                        attempt_count INTEGER NOT NULL,
                        worker_pid INTEGER,
                        created_at DATETIME NOT NULL,
                        started_at DATETIME,
                        finished_at DATETIME,
                        PRIMARY KEY (id),
                        FOREIGN KEY(project_id) REFERENCES projects (id),
                        FOREIGN KEY(interview_id) REFERENCES interviews (id)
                    )
                    """
                )
                raw.execute(
                    """
                    INSERT INTO processing_jobs_legacy (
                        id, project_id, interview_id, question_id, job_type, status,
                        progress_json, result_json, error_message, attempt_count,
                        worker_pid, created_at, started_at, finished_at
                    )
                    SELECT
                        id, project_id, interview_id, question_id, job_type, status,
                        progress_json, result_json, error_message, attempt_count,
                        worker_pid, created_at, started_at, finished_at
                    FROM processing_jobs
                    """
                )
                legacy_orphan_id = preserved_job_id + 1000
                raw.execute(
                    """
                    INSERT INTO processing_jobs_legacy (
                        id, project_id, interview_id, question_id, job_type, status,
                        progress_json, result_json, error_message, attempt_count,
                        worker_pid, created_at, started_at, finished_at
                    ) VALUES (?, ?, NULL, ?, 'analyze_question', 'succeeded',
                              NULL, NULL, NULL, 0, NULL, ?, NULL, NULL)
                    """,
                    (
                        legacy_orphan_id,
                        project_id,
                        999999999,
                        "2026-09-12 00:00:00",
                    ),
                )
                raw.execute("DROP TABLE processing_jobs")
                raw.execute(
                    "ALTER TABLE processing_jobs_legacy RENAME TO processing_jobs"
                )
                raw.commit()
            finally:
                raw.close()

            app_after_upgrade = create_app()
            app_after_upgrade.config["TESTING"] = True

            with app_after_upgrade.app_context():
                fk_rows = db.session.execute(
                    text("PRAGMA foreign_key_list(processing_jobs)")
                ).all()
                question_fk_present = any(
                    str(row[2]) == "interview_flow_questions"
                    and str(row[3]) == "question_id"
                    and str(row[4]) == "id"
                    for row in fk_rows
                )
                failures += check(
                    "legacy processing_jobs schema gains question_id FK",
                    question_fk_present,
                    f"foreign_keys={len(fk_rows)}",
                )

                preserved = db.session.get(ProcessingJob, preserved_job_id)
                legacy_orphan = db.session.get(ProcessingJob, legacy_orphan_id)
                failures += check(
                    "legacy FK migration preserves existing processing-job rows",
                    preserved is not None
                    and preserved.status == "succeeded"
                    and legacy_orphan is not None
                    and int(legacy_orphan.question_id or 0) == 999999999,
                )

                violations = db.session.execute(
                    text("PRAGMA foreign_key_check(processing_jobs)")
                ).all()
                failures += check(
                    "pre-existing legacy orphan remains visible to readiness audit",
                    any(int(row[1]) == legacy_orphan_id for row in violations),
                    f"violations={len(violations)}",
                )

                new_invalid_rejected = False
                db.session.add(ProcessingJob(
                    project_id=project_id,
                    question_id=888888888,
                    job_type="analyze_question",
                    status="pending",
                ))
                try:
                    db.session.commit()
                except IntegrityError:
                    new_invalid_rejected = True
                    db.session.rollback()
                failures += check(
                    "upgraded question_id FK rejects new orphan job",
                    new_invalid_rejected,
                )

                db.session.remove()
                db.engine.dispose()
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
