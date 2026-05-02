from datetime import datetime, timezone
from models import db


class GeneratedFile(db.Model):
    __tablename__ = "generated_files"

    id                     = db.Column(db.Integer, primary_key=True)
    project_id             = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    interview_id           = db.Column(db.Integer, db.ForeignKey("interviews.id"))  # NULL = 全体出力
    # verbatim / formatted_sheet / analysis / report / integrated
    file_type              = db.Column(db.Text, nullable=False)
    # docx / xlsx / csv
    file_format            = db.Column(db.Text, nullable=False)
    original_filename      = db.Column(db.Text)
    stored_path            = db.Column(db.Text, nullable=False)
    generation_params_json = db.Column(db.Text)
    created_at             = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project   = db.relationship("Project",   back_populates="generated_files")
    interview = db.relationship("Interview", back_populates="generated_files")
