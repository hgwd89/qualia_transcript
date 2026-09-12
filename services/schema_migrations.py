from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine


@dataclass(frozen=True)
class ProcessingJobQuestionFkMigration:
    rebuilt: bool
    preserved_rows: int = 0
    existing_violation_count: int = 0


def _has_question_fk(cursor) -> bool:
    for row in cursor.execute("PRAGMA foreign_key_list(processing_jobs)").fetchall():
        # (id, seq, table, from, to, on_update, on_delete, match)
        if str(row[2]) == "interview_flow_questions" and str(row[3]) == "question_id" and str(row[4]) == "id":
            return True
    return False


def ensure_processing_job_question_fk(engine: Engine) -> ProcessingJobQuestionFkMigration:
    """Repair the legacy SQLite processing_jobs schema without discarding rows.

    Older installations added ``question_id`` with ``ALTER TABLE ... ADD COLUMN``
    and therefore never received the model's foreign-key constraint. SQLite cannot
    add that constraint in place, so startup rebuilds only this table when needed.

    Existing orphan question IDs are intentionally preserved. Foreign-key checks
    are disabled only for the rebuild transaction, then restored. The normal
    production-readiness audit can surface pre-existing violations while all new
    writes are constrained after migration.
    """
    if engine.dialect.name != "sqlite":
        return ProcessingJobQuestionFkMigration(rebuilt=False)

    raw = engine.raw_connection()
    cursor = raw.cursor()
    previous_fk_setting = 1
    try:
        raw.rollback()
        exists = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='processing_jobs'"
        ).fetchone()
        if not exists:
            return ProcessingJobQuestionFkMigration(rebuilt=False)

        columns = {
            str(row[1])
            for row in cursor.execute("PRAGMA table_info(processing_jobs)").fetchall()
        }
        if "question_id" in columns and _has_question_fk(cursor):
            return ProcessingJobQuestionFkMigration(rebuilt=False)

        preserved_objects = [
            str(row[0])
            for row in cursor.execute(
                """
                SELECT sql
                FROM sqlite_master
                WHERE tbl_name='processing_jobs'
                  AND type IN ('index', 'trigger')
                  AND sql IS NOT NULL
                ORDER BY type, name
                """
            ).fetchall()
            if row[0]
        ]
        before_count = int(
            cursor.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()[0]
        )
        previous_fk_setting = int(
            cursor.execute("PRAGMA foreign_keys").fetchone()[0] or 0
        )

        # PRAGMA foreign_keys can only change outside a transaction.
        raw.rollback()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute("DROP TABLE IF EXISTS processing_jobs__fk_migration")
        cursor.execute(
            """
            CREATE TABLE processing_jobs__fk_migration (
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
                FOREIGN KEY(interview_id) REFERENCES interviews (id),
                FOREIGN KEY(question_id) REFERENCES interview_flow_questions (id)
            )
            """
        )

        question_expr = "question_id" if "question_id" in columns else "NULL"
        cursor.execute(
            f"""
            INSERT INTO processing_jobs__fk_migration (
                id,
                project_id,
                interview_id,
                question_id,
                job_type,
                status,
                progress_json,
                result_json,
                error_message,
                attempt_count,
                worker_pid,
                created_at,
                started_at,
                finished_at
            )
            SELECT
                id,
                project_id,
                interview_id,
                {question_expr},
                job_type,
                status,
                progress_json,
                result_json,
                error_message,
                attempt_count,
                worker_pid,
                created_at,
                started_at,
                finished_at
            FROM processing_jobs
            """
        )
        copied_count = int(
            cursor.execute(
                "SELECT COUNT(*) FROM processing_jobs__fk_migration"
            ).fetchone()[0]
        )
        if copied_count != before_count:
            raise RuntimeError(
                "processing_jobs FK migration row-count mismatch: "
                f"before={before_count} copied={copied_count}"
            )

        cursor.execute("DROP TABLE processing_jobs")
        cursor.execute(
            "ALTER TABLE processing_jobs__fk_migration RENAME TO processing_jobs"
        )
        for sql in preserved_objects:
            cursor.execute(sql)
        raw.commit()

        cursor.execute(
            f"PRAGMA foreign_keys={'ON' if previous_fk_setting else 'OFF'}"
        )
        violations = cursor.execute(
            "PRAGMA foreign_key_check(processing_jobs)"
        ).fetchall()
        return ProcessingJobQuestionFkMigration(
            rebuilt=True,
            preserved_rows=copied_count,
            existing_violation_count=len(violations),
        )
    except Exception:
        raw.rollback()
        try:
            cursor.execute(
                f"PRAGMA foreign_keys={'ON' if previous_fk_setting else 'OFF'}"
            )
        except Exception:
            pass
        raise
    finally:
        cursor.close()
        raw.close()
