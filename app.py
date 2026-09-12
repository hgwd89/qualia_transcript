import os
from datetime import datetime
from flask import Flask, render_template, g
import config
from models import db

from models.project        import Project
from models.participant    import Participant, ParticipantAttribute
from models.interview_flow import InterviewFlow, InterviewFlowSection, InterviewFlowQuestion
from models.interview      import Interview, MediaFile, Transcription
from models.segment        import Segment, UtteranceMapping
from models.segment_flag   import SegmentFlag
from models.speaker_assignment import SpeakerAssignment
from models.analysis       import AIAnalysis
from models.generated_file import GeneratedFile
from models.setting        import AppSetting
from models.processing_job import ProcessingJob

from routes.projects      import bp as projects_bp
from routes.participants  import bp as participants_bp
from routes.flows         import bp as flows_bp
from routes.interviews    import bp as interviews_bp
from routes.transcribe    import bp as transcribe_bp
from routes.analyze       import bp as analyze_bp
from routes.outputs       import bp as outputs_bp
from routes.settings      import bp as settings_bp
from routes.analysis_view import bp as analysis_view_bp


def _run_migrations(app):
    """
    既存 SQLite DB への後付けカラム・制約追加。
    inspect で既存スキーマを確認し、必要な変更だけを実行する。
    """
    from sqlalchemy import text, inspect as sa_inspect
    with app.app_context():
        inspector = sa_inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        if "segment_flags" not in existing_tables:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS segment_flags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    segment_id INTEGER NOT NULL,
                    flag_type TEXT NOT NULL,
                    note TEXT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(segment_id) REFERENCES segments(id)
                )
            """))
            db.session.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_segment_flag_type ON segment_flags(segment_id, flag_type)"
            ))
            db.session.commit()
            inspector = sa_inspect(db.engine)
            existing_tables = set(inspector.get_table_names())
        if "speaker_assignments" not in existing_tables:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS speaker_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interview_id INTEGER NOT NULL,
                    speaker_label TEXT NOT NULL,
                    speaker_role TEXT DEFAULT 'unknown',
                    participant_id INTEGER,
                    note TEXT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    FOREIGN KEY(interview_id) REFERENCES interviews(id),
                    FOREIGN KEY(participant_id) REFERENCES participants(id)
                )
            """))
            db.session.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_speaker_assignment_label ON speaker_assignments(interview_id, speaker_label)"
            ))
            db.session.commit()
            inspector = sa_inspect(db.engine)
            existing_tables = set(inspector.get_table_names())

        existing_cols = {c["name"] for c in inspector.get_columns("projects")}
        pending = [
            ("method", "ALTER TABLE projects ADD COLUMN method TEXT DEFAULT 'DI'"),
            ("status", "ALTER TABLE projects ADD COLUMN status TEXT DEFAULT 'draft'"),
            ("research_theme", "ALTER TABLE projects ADD COLUMN research_theme TEXT"),
            ("research_category", "ALTER TABLE projects ADD COLUMN research_category TEXT DEFAULT 'general'"),
            ("glossary_profile", "ALTER TABLE projects ADD COLUMN glossary_profile TEXT DEFAULT 'general'"),
            ("deliverable_type", "ALTER TABLE projects ADD COLUMN deliverable_type TEXT DEFAULT 'verbatim_and_sheet'"),
            ("confidentiality_level", "ALTER TABLE projects ADD COLUMN confidentiality_level TEXT DEFAULT 'standard'"),
        ]
        for col_name, stmt in pending:
            if col_name not in existing_cols:
                db.session.execute(text(stmt))
                db.session.commit()

        inspector = sa_inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        if "ai_analyses" in existing_tables:
            analysis_cols = {c["name"] for c in inspector.get_columns("ai_analyses")}
            analysis_pending = [
                (
                    "review_status",
                    "ALTER TABLE ai_analyses ADD COLUMN review_status TEXT NOT NULL DEFAULT 'draft'",
                ),
                ("review_note", "ALTER TABLE ai_analyses ADD COLUMN review_note TEXT"),
                ("reviewed_at", "ALTER TABLE ai_analyses ADD COLUMN reviewed_at DATETIME"),
            ]
            for col_name, stmt in analysis_pending:
                if col_name not in analysis_cols:
                    db.session.execute(text(stmt))
                    db.session.commit()

        inspector = sa_inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        if "processing_jobs" in existing_tables:
            from services.schema_migrations import ensure_processing_job_question_fk

            migration = ensure_processing_job_question_fk(db.engine)
            if migration.rebuilt:
                app.logger.info(
                    "rebuilt legacy processing_jobs question FK: rows=%s existing_violations=%s",
                    migration.preserved_rows,
                    migration.existing_violation_count,
                )


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"]                     = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"]        = config.DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"]             = config.MAX_UPLOAD_BYTES
    app.config["MAX_UPLOAD_BYTES"]               = config.MAX_UPLOAD_BYTES

    db.init_app(app)

    app.register_blueprint(projects_bp)
    app.register_blueprint(participants_bp)
    app.register_blueprint(flows_bp)
    app.register_blueprint(interviews_bp)
    app.register_blueprint(transcribe_bp)
    app.register_blueprint(analyze_bp)
    app.register_blueprint(outputs_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(analysis_view_bp)

    @app.context_processor
    def inject_globals():
        return {"SERVICE_NAME": config.SERVICE_NAME, "now": datetime.now()}

    with app.app_context():
        db.create_all()
        os.makedirs(config.UPLOAD_DIR, exist_ok=True)
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    _run_migrations(app)

    # Existing installations may contain plaintext API credentials from older
    # versions. On Windows, migrate them to current-user DPAPI storage at startup.
    with app.app_context():
        try:
            from services.secret_store import migrate_legacy_plaintext_secrets
            migrated = migrate_legacy_plaintext_secrets()
            if migrated.migrated_keys:
                app.logger.info(
                    "migrated legacy plaintext secret settings to DPAPI: %s",
                    ", ".join(migrated.migrated_keys),
                )
        except Exception:
            # Preserve startup/data access if Windows credential protection fails;
            # readiness/security checks can then surface the remaining plaintext.
            app.logger.exception("secret setting DPAPI migration failed")

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(
        host=config.APP_HOST,
        port=config.APP_PORT,
        debug=config.APP_DEBUG,
        use_reloader=False,
    )
