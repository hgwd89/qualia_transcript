from __future__ import annotations

from sqlalchemy import and_, or_, text

from models import db
from models.interview import Interview
from models.interview_flow import InterviewFlow
from models.processing_job import ProcessingJob
from models.project import Project
from services.job_recovery import recover_stale_jobs
from services.processing_jobs import ACTIVE_STATUSES
from services.research_input_guard import ResearchInputWriteBlocked


_PROJECT_FLOW_READERS = {
    "project_pipeline",
    "analyze_integrated",
}
_FLOW_INTERVIEW_READERS = {
    "map",
}


def _require_clean_session() -> None:
    """Flow fencing may discard read transactions, never pending writes."""
    if db.session.new or db.session.dirty or db.session.deleted:
        raise RuntimeError("flow input write fencing requires a clean database session")


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


def begin_flow_input_write(
    project_id: int,
    flow_id: int | None = None,
    *,
    affects_question_set: bool = False,
) -> Project | InterviewFlow:
    """Serialize canonical flow/question mutations against durable readers.

    Job admission and this guard both acquire SQLite ``BEGIN IMMEDIATE`` before
    their final active-job decision. A flow mutation therefore either commits
    before a conflicting job is admitted, or observes that active job and fails
    closed.

    Project-wide pipeline/integrated jobs consume the project flow set and flow
    structure, so every flow mutation conflicts with them. ``map`` consumes the
    complete question set for its interview flow; writes that add/remove/change
    questions set ``affects_question_set=True`` and conflict only with mapping
    jobs whose interviews are assigned to that exact flow.

    On success the caller owns the write transaction and must commit or roll it
    back after re-reading and mutating canonical rows.
    """
    project_id = int(project_id)
    flow_id = int(flow_id) if flow_id is not None else None

    # A dead worker must not hold the research schema hostage. Recovery performs
    # its own generation-fenced compare-and-swap before this function reserves
    # the final admission/write decision.
    recover_stale_jobs(project_id=project_id)

    try:
        _begin_immediate()
        project = db.session.get(Project, project_id)
        if project is None:
            db.session.rollback()
            raise ValueError("project not found")

        flow = None
        if flow_id is not None:
            flow = (
                InterviewFlow.query
                .filter_by(id=flow_id, project_id=project_id)
                .first()
            )
            if flow is None:
                db.session.rollback()
                raise ValueError("flow not found in project")

        conflict_scope = ProcessingJob.job_type.in_(_PROJECT_FLOW_READERS)

        if flow is not None and affects_question_set:
            interview_ids = [
                int(value)
                for (value,) in (
                    db.session.query(Interview.id)
                    .filter_by(project_id=project_id, flow_id=flow_id)
                    .all()
                )
            ]
            if interview_ids:
                conflict_scope = or_(
                    conflict_scope,
                    and_(
                        ProcessingJob.job_type.in_(_FLOW_INTERVIEW_READERS),
                        ProcessingJob.interview_id.in_(interview_ids),
                    ),
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
        return flow if flow is not None else project
    except ResearchInputWriteBlocked:
        raise
    except Exception:
        db.session.rollback()
        raise
