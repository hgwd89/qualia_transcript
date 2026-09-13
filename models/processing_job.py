import json
from datetime import datetime, timezone

from models import db


class ProcessingJob(db.Model):
    __tablename__ = "processing_jobs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    interview_id = db.Column(db.Integer, db.ForeignKey("interviews.id"))
    question_id = db.Column(db.Integer, db.ForeignKey("interview_flow_questions.id"))
    # transcribe / map / analyze / analyze_semantic / analyze_question / analyze_cross / analyze_integrated / project_pipeline
    job_type = db.Column(db.Text, nullable=False)
    # pending / running / succeeded / failed
    status = db.Column(db.Text, nullable=False, default="pending")
    progress_json = db.Column(db.Text)
    request_json = db.Column(db.Text)
    result_json = db.Column(db.Text)
    error_message = db.Column(db.Text)
    attempt_count = db.Column(db.Integer, nullable=False, default=0)
    worker_pid = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)

    project = db.relationship("Project")
    interview = db.relationship("Interview")
    question = db.relationship("InterviewFlowQuestion")

    def _json_value(self, raw):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "interview_id": self.interview_id,
            "question_id": self.question_id,
            "job_type": self.job_type,
            "status": self.status,
            "progress": self._json_value(self.progress_json),
            "request": self._json_value(self.request_json),
            "result": self._json_value(self.result_json),
            "error_message": self.error_message,
            "attempt_count": self.attempt_count,
            "worker_pid": self.worker_pid,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }
