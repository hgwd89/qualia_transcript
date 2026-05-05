from datetime import datetime, timezone
from models import db


class Interview(db.Model):
    __tablename__ = "interviews"

    id               = db.Column(db.Integer, primary_key=True)
    project_id       = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    participant_id   = db.Column(db.Integer, db.ForeignKey("participants.id"))
    flow_id          = db.Column(db.Integer, db.ForeignKey("interview_flows.id"))
    interview_date   = db.Column(db.Date)
    interviewer_name = db.Column(db.Text)
    location         = db.Column(db.Text)
    # pending / transcribed / mapped / analyzed / done / error
    status           = db.Column(db.Text, default="pending")
    notes            = db.Column(db.Text)
    created_at       = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    project     = db.relationship("Project",       back_populates="interviews")
    participant = db.relationship("Participant",    back_populates="interviews")
    flow        = db.relationship("InterviewFlow",  back_populates="interviews")
    media_files = db.relationship("MediaFile",      back_populates="interview",
                                  cascade="all, delete-orphan")
    segments    = db.relationship("Segment",        back_populates="interview",
                                  cascade="all, delete-orphan", order_by="Segment.seq")
    ai_analyses = db.relationship("AIAnalysis",     back_populates="interview",
                                  cascade="all, delete-orphan")
    generated_files = db.relationship("GeneratedFile", back_populates="interview",
                                      cascade="all, delete-orphan")
    speaker_assignments = db.relationship("SpeakerAssignment", back_populates="interview",
                                          cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "participant_id": self.participant_id,
            "flow_id": self.flow_id,
            "interview_date": self.interview_date.isoformat() if self.interview_date else None,
            "interviewer_name": self.interviewer_name,
            "location": self.location,
            "status": self.status,
            "notes": self.notes,
        }


class MediaFile(db.Model):
    __tablename__ = "media_files"

    id                = db.Column(db.Integer, primary_key=True)
    interview_id      = db.Column(db.Integer, db.ForeignKey("interviews.id"), nullable=False)
    original_filename = db.Column(db.Text, nullable=False)
    stored_path       = db.Column(db.Text, nullable=False)
    file_type         = db.Column(db.Text)          # audio / video
    mime_type         = db.Column(db.Text)
    duration_sec      = db.Column(db.Float)
    file_size_bytes   = db.Column(db.Integer)
    uploaded_at       = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    interview      = db.relationship("Interview",     back_populates="media_files")
    transcriptions = db.relationship("Transcription", back_populates="media_file",
                                     cascade="all, delete-orphan")


class Transcription(db.Model):
    __tablename__ = "transcriptions"

    id            = db.Column(db.Integer, primary_key=True)
    media_file_id = db.Column(db.Integer, db.ForeignKey("media_files.id"), nullable=False)
    whisper_model = db.Column(db.Text)
    language      = db.Column(db.Text, default="ja")
    # pending / running / done / error
    status        = db.Column(db.Text, default="pending")
    word_count    = db.Column(db.Integer)
    started_at    = db.Column(db.DateTime)
    completed_at  = db.Column(db.DateTime)
    error_message = db.Column(db.Text)

    media_file = db.relationship("MediaFile", back_populates="transcriptions")
    segments   = db.relationship("Segment",   back_populates="transcription",
                                 cascade="all, delete-orphan")
