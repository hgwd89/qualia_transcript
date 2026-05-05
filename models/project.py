from datetime import datetime, timezone
from models import db


class Project(db.Model):
    __tablename__ = "projects"

    id                 = db.Column(db.Integer, primary_key=True)
    name               = db.Column(db.Text, nullable=False)
    client             = db.Column(db.Text)
    research_theme     = db.Column(db.Text)
    research_category  = db.Column(db.Text, default="general")
    glossary_profile   = db.Column(db.Text, default="general")
    research_objective = db.Column(db.Text)
    description        = db.Column(db.Text)
    method             = db.Column(db.Text, default="DI")     # DI / FGI
    deliverable_type   = db.Column(db.Text, default="verbatim_and_sheet")
    confidentiality_level = db.Column(db.Text, default="standard")
    status             = db.Column(db.Text, default="draft")  # draft / in_progress / completed
    created_at         = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at         = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc),
                                   onupdate=lambda: datetime.now(timezone.utc))

    participants    = db.relationship("Participant",    back_populates="project", cascade="all, delete-orphan")
    interview_flows = db.relationship("InterviewFlow", back_populates="project", cascade="all, delete-orphan")
    interviews      = db.relationship("Interview",     back_populates="project", cascade="all, delete-orphan")
    generated_files = db.relationship("GeneratedFile", back_populates="project", cascade="all, delete-orphan")
    ai_analyses     = db.relationship("AIAnalysis",    back_populates="project", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "client": self.client,
            "research_theme": self.research_theme,
            "research_category": self.research_category,
            "glossary_profile": self.glossary_profile,
            "research_objective": self.research_objective,
            "description": self.description,
            "method": self.method,
            "deliverable_type": self.deliverable_type,
            "confidentiality_level": self.confidentiality_level,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
