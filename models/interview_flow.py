from datetime import datetime, timezone
from models import db


class InterviewFlow(db.Model):
    __tablename__ = "interview_flows"

    id         = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    title      = db.Column(db.Text, nullable=False)
    version    = db.Column(db.Text, default="1.0")
    notes      = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project    = db.relationship("Project",               back_populates="interview_flows")
    sections   = db.relationship("InterviewFlowSection",  back_populates="flow",
                                 cascade="all, delete-orphan", order_by="InterviewFlowSection.seq")
    interviews = db.relationship("Interview",             back_populates="flow")

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "sections": [s.to_dict() for s in self.sections],
        }


class InterviewFlowSection(db.Model):
    __tablename__ = "interview_flow_sections"

    id          = db.Column(db.Integer, primary_key=True)
    flow_id     = db.Column(db.Integer, db.ForeignKey("interview_flows.id"), nullable=False)
    title       = db.Column(db.Text, nullable=False)
    description = db.Column(db.Text)
    seq         = db.Column(db.Integer, nullable=False)

    flow      = db.relationship("InterviewFlow",         back_populates="sections")
    questions = db.relationship("InterviewFlowQuestion", back_populates="section",
                                cascade="all, delete-orphan", order_by="InterviewFlowQuestion.seq")

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "seq": self.seq,
            "questions": [q.to_dict() for q in self.questions],
        }


class InterviewFlowQuestion(db.Model):
    __tablename__ = "interview_flow_questions"

    id              = db.Column(db.Integer, primary_key=True)
    section_id      = db.Column(db.Integer, db.ForeignKey("interview_flow_sections.id"), nullable=False)
    question_code   = db.Column(db.Text)                     # Q1-1, Q2-3 ...
    question_text   = db.Column(db.Text, nullable=False)
    question_type   = db.Column(db.Text, default="open")     # open/probe/closing/screener
    is_key_question = db.Column(db.Boolean, default=False)
    seq             = db.Column(db.Integer, nullable=False)

    section          = db.relationship("InterviewFlowSection", back_populates="questions")
    utterance_mappings = db.relationship("UtteranceMapping",   back_populates="question",
                                         cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "question_code": self.question_code,
            "question_text": self.question_text,
            "question_type": self.question_type,
            "is_key_question": self.is_key_question,
            "seq": self.seq,
        }
