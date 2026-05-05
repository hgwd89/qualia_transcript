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
    既存 SQLite DB への後付けカラム追加。
    inspect でカラム存在を確認してから ALTER TABLE を実行するため冪等。
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


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"]                    = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"]       = config.DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"]            = config.MAX_UPLOAD_BYTES
    app.config["MAX_UPLOAD_BYTES"]              = config.MAX_UPLOAD_BYTES

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

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
