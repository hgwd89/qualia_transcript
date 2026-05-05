from datetime import datetime, timezone
from models import db


class SegmentFlag(db.Model):
    __tablename__ = "segment_flags"
    __table_args__ = (
        db.UniqueConstraint("segment_id", "flag_type", name="uq_segment_flag_type"),
    )

    id = db.Column(db.Integer, primary_key=True)
    segment_id = db.Column(db.Integer, db.ForeignKey("segments.id"), nullable=False, index=True)
    flag_type = db.Column(db.Text, nullable=False)
    note = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    segment = db.relationship("Segment", back_populates="segment_flags")

    def to_dict(self):
        return {
            "id": self.id,
            "segment_id": self.segment_id,
            "flag_type": self.flag_type,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
