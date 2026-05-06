from datetime import datetime, timezone
from models import db


class APIUsageLog(db.Model):
    __tablename__ = "api_usage_logs"
    __table_args__ = (
        db.Index("ix_api_usage_logs_created_at", "created_at"),
        db.Index("ix_api_usage_logs_provider_operation", "provider", "operation_type"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), index=True)
    interview_id = db.Column(db.Integer, db.ForeignKey("interviews.id"), index=True)
    analysis_id = db.Column(db.Integer, db.ForeignKey("ai_analyses.id"), index=True)
    provider = db.Column(db.Text, nullable=False)
    model = db.Column(db.Text, nullable=False)
    operation_type = db.Column(db.Text, nullable=False)
    request_count = db.Column(db.Integer, nullable=False, default=1)
    input_tokens = db.Column(db.Integer)
    output_tokens = db.Column(db.Integer)
    total_tokens = db.Column(db.Integer)
    audio_duration_sec = db.Column(db.Float)
    estimated_cost = db.Column(db.Float)
    success = db.Column(db.Boolean, nullable=False, default=True)
    error_message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project = db.relationship("Project")
    interview = db.relationship("Interview")
    analysis = db.relationship("AIAnalysis")
