import sqlite3
import sys
import tempfile
from pathlib import Path


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return ok


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    import config

    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }

    app = None
    db = None
    with tempfile.TemporaryDirectory(prefix="qualia_analysis_migration_") as tmp:
        tmp_dir = Path(tmp)
        db_path = tmp_dir / "legacy.db"

        # Simulate the pre-review-state AIAnalysis schema. create_all() must leave
        # the existing table in place, then _run_migrations() must add the columns.
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE ai_analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER,
                    interview_id INTEGER,
                    question_id INTEGER,
                    analysis_type TEXT NOT NULL,
                    title TEXT,
                    summary_text TEXT,
                    content_json TEXT,
                    model_used TEXT,
                    created_at DATETIME
                )
                """
            )
            conn.execute(
                """
                INSERT INTO ai_analyses (
                    project_id, interview_id, question_id, analysis_type,
                    title, summary_text, content_json, model_used
                ) VALUES (NULL, NULL, NULL, 'legacy', 'Legacy', '', '{}', 'legacy-model')
                """
            )
            conn.commit()
        finally:
            conn.close()

        try:
            config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
            config.UPLOAD_DIR = str(tmp_dir / "uploads")
            config.OUTPUT_DIR = str(tmp_dir / "outputs")

            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from sqlalchemy import inspect as sa_inspect

            app = create_app()

            with app.app_context():
                columns = {c["name"] for c in sa_inspect(db.engine).get_columns("ai_analyses")}
                expected = {"review_status", "review_note", "reviewed_at"}
                ok = expected.issubset(columns)
                failures += 0 if print_result(
                    "legacy ai_analyses migration adds review columns",
                    ok,
                    ",".join(sorted(expected & columns)),
                ) else 1

                legacy = AIAnalysis.query.filter_by(analysis_type="legacy").first()
                failures += 0 if print_result(
                    "legacy AIAnalysis defaults to draft after migration",
                    legacy is not None and legacy.review_status == "draft",
                    str(legacy.review_status if legacy else None),
                ) else 1
        except Exception as e:
            failures += 0 if print_result(
                "analysis review migration smoke",
                False,
                f"{type(e).__name__}: {e}",
            ) else 1
        finally:
            if app is not None and db is not None:
                try:
                    with app.app_context():
                        db.session.remove()
                        db.engine.dispose()
                except Exception:
                    pass
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]

    if failures == 0:
        print("\nSummary: PASS")
        return 0

    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
