from datetime import datetime, timezone
from models import db


class Participant(db.Model):
    __tablename__ = "participants"

    id               = db.Column(db.Integer, primary_key=True)
    project_id       = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    participant_code = db.Column(db.Text, nullable=False)   # P01, P02 ...
    display_name     = db.Column(db.Text)
    created_at       = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project    = db.relationship("Project",    back_populates="participants")
    attributes = db.relationship("ParticipantAttribute", back_populates="participant",
                                 cascade="all, delete-orphan", order_by="ParticipantAttribute.display_order")
    interviews = db.relationship("Interview",  back_populates="participant")
    segments   = db.relationship("Segment",    back_populates="participant")

    def to_dict(self):
        return {
            "id": self.id,
            "participant_code": self.participant_code,
            "display_name": self.display_name,
            "attributes": [a.to_dict() for a in self.attributes],
        }


class ParticipantAttribute(db.Model):
    __tablename__ = "participant_attributes"

    id              = db.Column(db.Integer, primary_key=True)
    participant_id  = db.Column(db.Integer, db.ForeignKey("participants.id"), nullable=False)
    attribute_key   = db.Column(db.Text, nullable=False)   # 性別, 年代, 職業 ...
    attribute_value = db.Column(db.Text)
    display_order   = db.Column(db.Integer, default=0)

    participant = db.relationship("Participant", back_populates="attributes")

    def to_dict(self):
        return {"key": self.attribute_key, "value": self.attribute_value}
