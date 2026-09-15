from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    original_uri = config.DATABASE_URI

    with tempfile.TemporaryDirectory(prefix="qualia_interview_collection_fencing_") as tmp:
        db_path = Path(tmp) / "collection-fencing.db"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        app = None
        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import InterviewFlow
            from models.participant import Participant
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.research_input_guard import begin_interview_collection_write

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Interview collection fencing")
                db.session.add(project)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Participant 1",
                )
                flow = InterviewFlow(
                    project_id=project.id,
                    title="Main flow",
                    version="1.0",
                )
                db.session.add_all([participant, flow])
                db.session.commit()

                project_id = int(project.id)
                participant_id = int(participant.id)
                flow_id = int(flow.id)

                def post_new(*, with_participant: bool = True):
                    data = {"flow_id": str(flow_id)}
                    if with_participant:
                        data["participant_id"] = str(participant_id)
                    return client.post(
                        f"/projects/{project_id}/interviews/new",
                        data=data,
                    )

                pipeline_job = ProcessingJob(
                    project_id=project_id,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(pipeline_job)
                db.session.commit()
                before = Interview.query.count()
                response = post_new(with_participant=True)
                failures += check(
                    "active project pipeline blocks new interview collection write",
                    response.status_code == 409 and Interview.query.count() == before,
                    f"status={response.status_code} interviews={Interview.query.count()}",
                )
                pipeline_job = db.session.get(ProcessingJob, int(pipeline_job.id))
                pipeline_job.status = "succeeded"
                db.session.commit()

                integrated_job = ProcessingJob(
                    project_id=project_id,
                    job_type="analyze_integrated",
                    status="pending",
                )
                db.session.add(integrated_job)
                db.session.commit()
                before = Interview.query.count()
                response = post_new(with_participant=True)
                failures += check(
                    "active integrated analysis blocks participant interview insertion",
                    response.status_code == 409 and Interview.query.count() == before,
                    f"status={response.status_code} interviews={Interview.query.count()}",
                )

                # Participant-less interviews do not enter integrated-analysis
                # source scope, so avoid over-locking that legitimate write.
                response = post_new(with_participant=False)
                failures += check(
                    "active integrated analysis does not over-block participant-less interview",
                    response.status_code == 302 and Interview.query.count() == before + 1,
                    f"status={response.status_code} interviews={Interview.query.count()}",
                )
                integrated_job = db.session.get(ProcessingJob, int(integrated_job.id))
                integrated_job.status = "succeeded"
                db.session.commit()

                cross_job = ProcessingJob(
                    project_id=project_id,
                    job_type="analyze_cross",
                    status="pending",
                )
                db.session.add(cross_job)
                db.session.commit()
                before = Interview.query.count()
                response = post_new(with_participant=True)
                failures += check(
                    "active cross analysis does not block empty new interview outside its provider source",
                    response.status_code == 302 and Interview.query.count() == before + 1,
                    f"status={response.status_code} interviews={Interview.query.count()}",
                )
                cross_job = db.session.get(ProcessingJob, int(cross_job.id))
                cross_job.status = "succeeded"
                db.session.commit()

                # Prove this is not a check-then-write guard. Once the collection
                # writer owns BEGIN IMMEDIATE, a competing durable-job admission
                # cannot publish a job row until that generation commits/rolls back.
                begin_interview_collection_write(
                    project_id,
                    affects_integrated_scope=False,
                )
                competing_write_blocked = False
                raw = sqlite3.connect(str(db_path), timeout=0.05)
                try:
                    try:
                        raw.execute(
                            """
                            INSERT INTO processing_jobs
                                (project_id, job_type, status, attempt_count, created_at)
                            VALUES (?, 'project_pipeline', 'pending', 0, CURRENT_TIMESTAMP)
                            """,
                            (project_id,),
                        )
                        raw.commit()
                    except sqlite3.OperationalError as exc:
                        competing_write_blocked = "locked" in str(exc).lower()
                        raw.rollback()
                finally:
                    raw.close()
                    db.session.rollback()
                failures += check(
                    "interview collection guard serializes competing job admission writes",
                    competing_write_blocked,
                )

                # After the reservation is released, normal creation remains valid.
                before = Interview.query.count()
                response = post_new(with_participant=True)
                failures += check(
                    "new interview creation succeeds when conflicting readers are absent",
                    response.status_code == 302 and Interview.query.count() == before + 1,
                    f"status={response.status_code} interviews={Interview.query.count()}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "interview collection input fencing smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if app is not None:
                try:
                    from models import db
                    with app.app_context():
                        db.session.remove()
                        db.engine.dispose()
                except Exception:
                    pass
            config.DATABASE_URI = original_uri

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
