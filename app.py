import os
from flask import Flask, render_template, g
import config
from models import db

# ── モデルの import（全テーブルを db に登録するために必要）
from models.project        import Project
from models.participant    import Participant, ParticipantAttribute
from models.interview_flow import InterviewFlow, InterviewFlowSection, InterviewFlowQuestion
from models.interview      import Interview, MediaFile, Transcription
from models.segment        import Segment, UtteranceMapping
from models.analysis       import AIAnalysis
from models.generated_file import GeneratedFile
from models.setting        import AppSetting

# ── Blueprint の import
from routes.projects      import bp as projects_bp
from routes.participants  import bp as participants_bp
from routes.flows         import bp as flows_bp
from routes.interviews    import bp as interviews_bp
from routes.transcribe    import bp as transcribe_bp
from routes.analyze       import bp as analyze_bp
from routes.outputs       import bp as outputs_bp
from routes.settings      import bp as settings_bp


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"]        = config.SECRET_KEY
    app.config["SQLALCHEMY_DATABASE_URI"] = config.DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES

    db.init_app(app)

    app.register_blueprint(projects_bp)
    app.register_blueprint(participants_bp)
    app.register_blueprint(flows_bp)
    app.register_blueprint(interviews_bp)
    app.register_blueprint(transcribe_bp)
    app.register_blueprint(analyze_bp)
    app.register_blueprint(outputs_bp)
    app.register_blueprint(settings_bp)

    # テンプレート全体で SERVICE_NAME を利用可能にする
    @app.context_processor
    def inject_globals():
        return {"SERVICE_NAME": config.SERVICE_NAME}

    with app.app_context():
        db.create_all()
        os.makedirs(config.UPLOAD_DIR, exist_ok=True)
        os.makedirs(config.OUTPUT_DIR, exist_ok=True)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(debug=True, port=5000)
