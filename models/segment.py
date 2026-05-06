from datetime import datetime, timezone
from models import db


class Segment(db.Model):
    __tablename__ = "segments"

    id               = db.Column(db.Integer, primary_key=True)
    transcription_id = db.Column(db.Integer, db.ForeignKey("transcriptions.id"))
    interview_id     = db.Column(db.Integer, db.ForeignKey("interviews.id"), nullable=False)
    participant_id   = db.Column(db.Integer, db.ForeignKey("participants.id"))  # NULL = インタビュアー等
    speaker_label    = db.Column(db.Text)       # SPEAKER_00, SPEAKER_01 ...
    # respondent / interviewer / unknown
    speaker_role     = db.Column(db.Text, default="unknown")
    start_sec        = db.Column(db.Float)
    end_sec          = db.Column(db.Float)
    text             = db.Column(db.Text, nullable=False)
    seq              = db.Column(db.Integer, nullable=False)
    created_at       = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    transcription     = db.relationship("Transcription",    back_populates="segments")
    interview         = db.relationship("Interview",        back_populates="segments")
    participant       = db.relationship("Participant",      back_populates="segments")
    utterance_mappings = db.relationship("UtteranceMapping", back_populates="segment",
                                         cascade="all, delete-orphan")
    segment_flags      = db.relationship("SegmentFlag",      back_populates="segment",
                                         cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "speaker_label": self.speaker_label,
            "speaker_role": self.speaker_role,
            "participant_id": self.participant_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "text": self.text,
            "seq": self.seq,
        }


class UtteranceMapping(db.Model):
    __tablename__ = "utterance_mappings"

    id               = db.Column(db.Integer, primary_key=True)
    segment_id       = db.Column(db.Integer, db.ForeignKey("segments.id"), nullable=False)
    question_id      = db.Column(db.Integer, db.ForeignKey("interview_flow_questions.id"))  # NULL = 未分類
    mapped_by        = db.Column(db.Text, default="ai")    # ai / manual
    confidence       = db.Column(db.Float)                 # 0.0〜1.0
    # high / medium / low
    confidence_level = db.Column(db.Text)
    is_unclassified  = db.Column(db.Boolean, default=False)
    notes            = db.Column(db.Text)
    created_at       = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    segment  = db.relationship("Segment",               back_populates="utterance_mappings")
    question = db.relationship("InterviewFlowQuestion", back_populates="utterance_mappings")

    def to_dict(self):
        return {
            "id": self.id,
            "segment_id": self.segment_id,
            "question_id": self.question_id,
            "mapped_by": self.mapped_by,
            "confidence": self.confidence,
            "confidence_level": self.confidence_level,
            "is_unclassified": self.is_unclassified,
        }
