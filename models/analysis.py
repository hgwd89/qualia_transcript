from datetime import datetime, timezone
from models import db


class AIAnalysis(db.Model):
    __tablename__ = "ai_analyses"

    id            = db.Column(db.Integer, primary_key=True)
    project_id    = db.Column(db.Integer, db.ForeignKey("projects.id"))
    interview_id  = db.Column(db.Integer, db.ForeignKey("interviews.id"))   # NULL = 統合分析
    question_id   = db.Column(db.Integer, db.ForeignKey("interview_flow_questions.id"))  # NULL = 全体
    # per_question / per_participant / theme / integrated
    analysis_type = db.Column(db.Text, nullable=False)
    title         = db.Column(db.Text)
    summary_text  = db.Column(db.Text)
    # JSON: { findings: [{point, evidence_quote, participant_codes, question_codes, confidence}],
    #         implications, unresolved }
    content_json  = db.Column(db.Text)
    model_used    = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    interview = db.relationship("Interview",              back_populates="ai_analyses")
    project   = db.relationship("Project",               back_populates="ai_analyses")
    question  = db.relationship("InterviewFlowQuestion")
