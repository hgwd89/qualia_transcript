from models.processing_job import ProcessingJob
from services.processing_jobs import ACTIVE_STATUSES


def find_conflicting_active_job(
    project_id: int,
    job_type: str,
    interview_id: int | None,
) -> ProcessingJob | None:
    """Return a different active job that would make concurrent work unsafe."""
    active = (
        ProcessingJob.query
        .filter_by(project_id=project_id)
        .filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
        .order_by(ProcessingJob.id.desc())
        .all()
    )

    for job in active:
        # Same requested scope/type is handled by create_or_get_active_job dedupe.
        if job.job_type == job_type and job.interview_id == interview_id:
            continue

        # A project pipeline is exclusive across the whole project.
        if job_type == "project_pipeline" or job.job_type == "project_pipeline":
            return job

        # Individual long-running operations are serialized per interview.
        if interview_id is not None and job.interview_id == interview_id:
            return job

    return None
