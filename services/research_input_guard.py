from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from models import db
from models.interview import Interview
from models.participant import Participant
from models.processing_job import ProcessingJob
from models.project import Project
from services.job_recovery import recover_stale_jobs
from services.processing_jobs import ACTIVE_STATUSES


_PROJECT_WIDE_INPUT_READERS = {
    "project_pipeline",
    "analyze_cross",
    "analyze_integrated",
}
_PARTICIPANT_INTERVIEW_READERS = {
    "analyze",
    "analyze_question",
}
_PROJECT_METADATA_READERS = {
    "project_pipeline",
    "analyze_integrated",
}


@dataclass(frozen=True)
class ResearchInputWriteBlocked(RuntimeError):
    active_job_ids: tuple[int, ...]

    def __str__(self) -> str:
        joined = ", ".join(str(value) for value in self.active_job_ids)
        return f"research input is locked by active processing job(s): {joined}"


def _require_clean_session() -> None:
    """Input fencing may discard read transactions, never pending writes."""
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("research input write fencing requires a clean database session")


def _begin_immediate() -> None:
    _require_clean_session()
    db.session.rollback()
    db.session.execute(text("BEGIN IMMEDIATE"))


def _raise_if_conflicts(conflicts: list[ProcessingJob]) -> None:
    if not conflicts:
        return
    job_ids = tuple(int(job.id) for job in conflicts)
    db.session.rollback()
    raise ResearchInputWriteBlocked(job_ids)


def begin_interview_input_write(interview_id: int) -> Interview:
    """Serialize one canonical interview-input mutation against durable jobs.

    Job admission and this guard both acquire SQLite ``BEGIN IMMEDIATE`` before
    inspecting the active-job set. Therefore either the input write commits
    before a job is admitted, or the input write observes the admitted job and
    is rejected. This closes the read-input -> provider-call -> result-commit
    TOCTOU window for mutable mapping/role inputs.

    On success the caller owns the write transaction and must commit or roll it
    back after re-reading and mutating the requested input rows.
    """
    interview_id = int(interview_id)
    interview = db.session.get(Interview, interview_id)
    if interview is None:
        raise ValueError("interview not found")
    project_id = int(interview.project_id)

    # Do not let a dead/stale worker block manual correction forever. Recovery
    # uses its own CAS fencing, then this function acquires the admission/write
    # reservation for the final active-set decision.
    recover_stale_jobs(project_id=project_id)

    try:
        _begin_immediate()
        interview = db.session.get(Interview, interview_id)
        if interview is None:
            db.session.rollback()
            raise ValueError("interview not found")

        conflicts = (
            ProcessingJob.query
            .filter_by(project_id=int(interview.project_id))
            .filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
            .filter(
                (ProcessingJob.interview_id == interview_id)
                | (ProcessingJob.job_type.in_(_PROJECT_WIDE_INPUT_READERS))
            )
            .order_by(ProcessingJob.id.asc())
            .all()
        )
        _raise_if_conflicts(conflicts)
        return interview
    except ResearchInputWriteBlocked:
        raise
    except Exception:
        db.session.rollback()
        raise


def begin_participant_input_write(project_id: int, participant_id: int) -> Participant:
    """Fence participant identity edits against analyses that consume it.

    Participant code/display name are provider inputs for participant-level,
    per-question, cross-participant, and integrated analysis. The guard blocks
    only interview-scoped analysis jobs for interviews owned by this participant,
    plus project-wide jobs that can read every participant in the project.
    """
    project_id = int(project_id)
    participant_id = int(participant_id)
    participant = (
        Participant.query
        .filter_by(id=participant_id, project_id=project_id)
        .first()
    )
    if participant is None:
        raise ValueError("participant not found in project")

    recover_stale_jobs(project_id=project_id)

    try:
        _begin_immediate()
        participant = (
            Participant.query
            .filter_by(id=participant_id, project_id=project_id)
            .first()
        )
        if participant is None:
            db.session.rollback()
            raise ValueError("participant not found in project")

        interview_ids = [
            int(value)
            for (value,) in (
                db.session.query(Interview.id)
                .filter_by(project_id=project_id, participant_id=participant_id)
                .all()
            )
        ]
        conflict_scope = ProcessingJob.job_type.in_(_PROJECT_WIDE_INPUT_READERS)
        if interview_ids:
            conflict_scope = conflict_scope | (
                ProcessingJob.interview_id.in_(interview_ids)
                & ProcessingJob.job_type.in_(_PARTICIPANT_INTERVIEW_READERS)
            )

        conflicts = (
            ProcessingJob.query
            .filter_by(project_id=project_id)
            .filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
            .filter(conflict_scope)
            .order_by(ProcessingJob.id.asc())
            .all()
        )
        _raise_if_conflicts(conflicts)
        return participant
    except ResearchInputWriteBlocked:
        raise
    except Exception:
        db.session.rollback()
        raise


def begin_project_metadata_write(project_id: int) -> Project:
    """Fence project metadata edits consumed by integrated analysis prompts."""
    project_id = int(project_id)
    project = db.session.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")

    recover_stale_jobs(project_id=project_id)

    try:
        _begin_immediate()
        project = db.session.get(Project, project_id)
        if project is None:
            db.session.rollback()
            raise ValueError("project not found")

        conflicts = (
            ProcessingJob.query
            .filter_by(project_id=project_id)
            .filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
            .filter(ProcessingJob.job_type.in_(_PROJECT_METADATA_READERS))
            .order_by(ProcessingJob.id.asc())
            .all()
        )
        _raise_if_conflicts(conflicts)
        return project
    except ResearchInputWriteBlocked:
        raise
    except Exception:
        db.session.rollback()
        raise
