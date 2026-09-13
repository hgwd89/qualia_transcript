from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
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

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_metadata_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'metadata-fencing.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.participant import Participant
            from models.processing_job import ProcessingJob
            from models.project import Project

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(
                    name="Metadata fencing smoke",
                    client="Original client",
                    research_objective="Original objective",
                )
                db.session.add(project)
                db.session.flush()
                p1 = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Original P01",
                )
                p2 = Participant(
                    project_id=project.id,
                    participant_code="P02",
                    display_name="Original P02",
                )
                db.session.add_all([p1, p2])
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=p1.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.commit()

                project_id = int(project.id)
                p1_id = int(p1.id)
                p2_id = int(p2.id)
                interview_id = int(interview.id)

                participant_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=interview_id,
                    job_type="analyze",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(participant_job)
                db.session.commit()
                participant_job_id = int(participant_job.id)

                response = client.post(
                    f"/projects/{project_id}/participants/{p1_id}/edit",
                    data={
                        "participant_code": "P99",
                        "display_name": "Changed P01",
                    },
                )
                db.session.expire_all()
                current_p1 = db.session.get(Participant, p1_id)
                failures += check(
                    "interview analysis blocks participant identity mutation",
                    response.status_code == 302
                    and current_p1.participant_code == "P01"
                    and current_p1.display_name == "Original P01",
                    f"status={response.status_code} participant={current_p1.to_dict()}",
                )

                participant_job = db.session.get(ProcessingJob, participant_job_id)
                participant_job.status = "succeeded"
                participant_job.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                response = client.post(
                    f"/projects/{project_id}/participants/{p1_id}/edit",
                    data={
                        "participant_code": "P99",
                        "display_name": "Changed P01",
                    },
                )
                db.session.expire_all()
                current_p1 = db.session.get(Participant, p1_id)
                failures += check(
                    "participant identity mutation proceeds after interview analysis is terminal",
                    response.status_code == 302
                    and current_p1.participant_code == "P99"
                    and current_p1.display_name == "Changed P01",
                    f"status={response.status_code} participant={current_p1.to_dict()}",
                )

                cross_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=None,
                    job_type="analyze_cross",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(cross_job)
                db.session.commit()
                cross_job_id = int(cross_job.id)

                response = client.post(
                    f"/projects/{project_id}/participants/{p2_id}/edit",
                    data={
                        "participant_code": "P88",
                        "display_name": "Changed P02",
                    },
                )
                db.session.expire_all()
                current_p2 = db.session.get(Participant, p2_id)
                failures += check(
                    "project-wide analysis blocks every participant identity mutation",
                    response.status_code == 302
                    and current_p2.participant_code == "P02"
                    and current_p2.display_name == "Original P02",
                    f"status={response.status_code} participant={current_p2.to_dict()}",
                )

                cross_job = db.session.get(ProcessingJob, cross_job_id)
                cross_job.status = "succeeded"
                cross_job.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                integrated_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=None,
                    job_type="analyze_integrated",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(integrated_job)
                db.session.commit()
                integrated_job_id = int(integrated_job.id)

                response = client.post(
                    f"/projects/{project_id}/edit",
                    data={
                        "name": "Changed project",
                        "client": "Changed client",
                        "research_objective": "Changed objective",
                    },
                )
                db.session.expire_all()
                current_project = db.session.get(Project, project_id)
                failures += check(
                    "integrated analysis blocks project prompt-metadata mutation",
                    response.status_code == 302
                    and current_project.name == "Metadata fencing smoke"
                    and current_project.client == "Original client"
                    and current_project.research_objective == "Original objective",
                    f"status={response.status_code} project={current_project.to_dict()}",
                )

                integrated_job = db.session.get(ProcessingJob, integrated_job_id)
                integrated_job.status = "succeeded"
                integrated_job.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                response = client.post(
                    f"/projects/{project_id}/edit",
                    data={
                        "name": "Changed project",
                        "client": "Changed client",
                        "research_objective": "Changed objective",
                    },
                )
                db.session.expire_all()
                current_project = db.session.get(Project, project_id)
                failures += check(
                    "project prompt-metadata mutation proceeds after integrated analysis is terminal",
                    response.status_code == 302
                    and current_project.name == "Changed project"
                    and current_project.client == "Changed client"
                    and current_project.research_objective == "Changed objective",
                    f"status={response.status_code} project={current_project.to_dict()}",
                )

                stale_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=None,
                    job_type="analyze_integrated",
                    status="pending",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(stale_job)
                db.session.commit()
                stale_job_id = int(stale_job.id)

                response = client.post(
                    f"/projects/{project_id}/edit",
                    data={
                        "name": "Recovered project",
                        "client": "Recovered client",
                        "research_objective": "Recovered objective",
                    },
                )
                db.session.expire_all()
                stale_after = db.session.get(ProcessingJob, stale_job_id)
                current_project = db.session.get(Project, project_id)
                failures += check(
                    "stale project-wide analysis is recovered before metadata write decision",
                    response.status_code == 302
                    and stale_after.status == "failed"
                    and current_project.name == "Recovered project"
                    and current_project.client == "Recovered client"
                    and current_project.research_objective == "Recovered objective",
                    f"status={response.status_code} job={stale_after.to_dict()}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "analysis metadata input fencing smoke",
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
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
