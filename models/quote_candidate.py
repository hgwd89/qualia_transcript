from datetime import datetime, timezone
from models import db


class QuoteCandidate(db.Model):
    __tablename__ = "quote_candidates"

    # quote_text は AI 生成文ではなく、Segment.text / reviewed_text 由来を保存する前提。
    id = db.Column(db.Integer, primary_key=True)
    quote_id = db.Column(db.Text, unique=True, nullable=False)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
    interview_id = db.Column(db.Integer, db.ForeignKey("interviews.id"), nullable=False, index=True)
    segment_id = db.Column(db.Integer, db.ForeignKey("segments.id"), nullable=False, index=True)
    participant_id = db.Column(db.Integer, db.ForeignKey("participants.id"))
    question_id = db.Column(db.Integer, db.ForeignKey("interview_flow_questions.id"))
    start_sec = db.Column(db.Float)
    end_sec = db.Column(db.Float)
    char_start = db.Column(db.Integer)
    char_end = db.Column(db.Integer)
    quote_text = db.Column(db.Text, nullable=False)
    status = db.Column(db.Text, nullable=False, default="candidate")
    # source: human / ai / flag / import
    source = db.Column(db.Text, nullable=False)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project")
    interview = db.relationship("Interview")
    segment = db.relationship("Segment")
    participant = db.relationship("Participant")
    question = db.relationship("InterviewFlowQuestion")
