from datetime import datetime, timezone
from models import db


class SpeakerAssignment(db.Model):
    __tablename__ = "speaker_assignments"
    __table_args__ = (
        db.UniqueConstraint("interview_id", "speaker_label", name="uq_speaker_assignment_label"),
    )

    id = db.Column(db.Integer, primary_key=True)
    interview_id = db.Column(db.Integer, db.ForeignKey("interviews.id"), nullable=False, index=True)
    speaker_label = db.Column(db.Text, nullable=False)
    # moderator / respondent / observer / unknown
    speaker_role = db.Column(db.Text, default="unknown")
    participant_id = db.Column(db.Integer, db.ForeignKey("participants.id"), nullable=True)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    interview = db.relationship("Interview", back_populates="speaker_assignments")
    participant = db.relationship("Participant")

    def to_dict(self):
        return {
            "id": self.id,
            "interview_id": self.interview_id,
            "speaker_label": self.speaker_label,
            "speaker_role": self.speaker_role,
            "participant_id": self.participant_id,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
