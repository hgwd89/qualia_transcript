from datetime import datetime, timezone
from models import db


class ReviewItem(db.Model):
    __tablename__ = "review_items"
    __table_args__ = (
        db.Index("ix_review_items_project_status", "project_id", "status"),
        db.Index("ix_review_items_interview_status", "interview_id", "status"),
        db.Index("ix_review_items_item_type_status", "item_type", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), index=True)
    interview_id = db.Column(db.Integer, db.ForeignKey("interviews.id"), index=True)
    item_type = db.Column(db.Text, nullable=False)
    target_type = db.Column(db.Text, nullable=False)
    target_id = db.Column(db.Integer, nullable=False)
    severity = db.Column(db.Text, nullable=False, default="medium")
    status = db.Column(db.Text, nullable=False, default="open")
    reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project")
    interview = db.relationship("Interview")
