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

    with tempfile.TemporaryDirectory(prefix="qualia_flow_input_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'flow-input-fencing.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.participant import Participant
            from models.processing_job import ProcessingJob
            from models.project import Project

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Flow input fencing smoke")
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Main flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section 1", seq=1)
                db.session.add(section)
                db.session.flush()
                q1 = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Question 1",
                    seq=1,
                )
                participant = Participant(project_id=project.id, participant_code="P01")
                db.session.add_all([q1, participant])
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    flow_id=flow.id,
                    status="transcribed",
                )
                db.session.add(interview)
                db.session.commit()

                project_id = int(project.id)
                flow_id = int(flow.id)
                section_id = int(section.id)
                interview_id = int(interview.id)

                map_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=interview_id,
                    job_type="map",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(map_job)
                db.session.commit()
                map_job_id = int(map_job.id)

                response = client.post(
                    f"/projects/{project_id}/flows/{flow_id}/sections/{section_id}/questions/new",
                    data={
                        "question_code": "Q2",
                        "question_text": "Question 2",
                        "question_type": "open",
                    },
                )
                db.session.expire_all()
                blocked_q2 = (
                    InterviewFlowQuestion.query
                    .join(InterviewFlowSection)
                    .filter(
                        InterviewFlowSection.flow_id == flow_id,
                        InterviewFlowQuestion.question_code == "Q2",
                    )
                    .first()
                )
                map_after_block = db.session.get(ProcessingJob, map_job_id)
                failures += check(
                    "same-flow active mapping blocks question-set mutation",
                    response.status_code == 302
                    and blocked_q2 is None
                    and map_after_block.status == "pending",
                    f"status={response.status_code} job={map_after_block.to_dict()}",
                )

                map_after_block.status = "succeeded"
                map_after_block.worker_pid = None
                map_after_block.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                response = client.post(
                    f"/projects/{project_id}/flows/{flow_id}/sections/{section_id}/questions/new",
                    data={
                        "question_code": "Q2",
                        "question_text": "Question 2",
                        "question_type": "open",
                    },
                )
                db.session.expire_all()
                q2 = (
                    InterviewFlowQuestion.query
                    .join(InterviewFlowSection)
                    .filter(
                        InterviewFlowSection.flow_id == flow_id,
                        InterviewFlowQuestion.question_code == "Q2",
                    )
                    .first()
                )
                failures += check(
                    "question-set mutation proceeds after mapping is terminal",
                    response.status_code == 302 and q2 is not None,
                    f"status={response.status_code}",
                )

                integrated_job = ProcessingJob(
                    project_id=project_id,
                    job_type="analyze_integrated",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(integrated_job)
                db.session.commit()
                integrated_job_id = int(integrated_job.id)

                flow_count_before = InterviewFlow.query.filter_by(project_id=project_id).count()
                response = client.post(
                    f"/projects/{project_id}/flows/new",
                    data={"title": "Second flow", "version": "1.0"},
                )
                db.session.expire_all()
                flow_count_blocked = InterviewFlow.query.filter_by(project_id=project_id).count()
                integrated_after_block = db.session.get(ProcessingJob, integrated_job_id)
                failures += check(
                    "project-wide integrated analysis blocks flow-set mutation",
                    response.status_code == 302
                    and flow_count_blocked == flow_count_before
                    and integrated_after_block.status == "pending",
                    f"status={response.status_code} before={flow_count_before} after={flow_count_blocked}",
                )

                section_count_before = InterviewFlowSection.query.filter_by(flow_id=flow_id).count()
                response = client.post(
                    f"/projects/{project_id}/flows/{flow_id}/sections/new",
                    data={"title": "Blocked section"},
                )
                db.session.expire_all()
                section_count_blocked = InterviewFlowSection.query.filter_by(flow_id=flow_id).count()
                failures += check(
                    "project-wide integrated analysis blocks flow-structure mutation",
                    response.status_code == 302
                    and section_count_blocked == section_count_before,
                    f"status={response.status_code} before={section_count_before} after={section_count_blocked}",
                )

                integrated_after_block.status = "succeeded"
                integrated_after_block.worker_pid = None
                integrated_after_block.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                response = client.post(
                    f"/projects/{project_id}/flows/new",
                    data={"title": "Second flow", "version": "1.0"},
                )
                db.session.expire_all()
                second_flow = InterviewFlow.query.filter_by(
                    project_id=project_id,
                    title="Second flow",
                ).first()
                failures += check(
                    "flow-set mutation proceeds after integrated analysis is terminal",
                    response.status_code == 302 and second_flow is not None,
                    f"status={response.status_code}",
                )

                participant2 = Participant(project_id=project_id, participant_code="P02")
                db.session.add(participant2)
                db.session.flush()
                interview2 = Interview(
                    project_id=project_id,
                    participant_id=participant2.id,
                    flow_id=second_flow.id,
                    status="transcribed",
                )
                db.session.add(interview2)
                db.session.commit()
                other_map_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=int(interview2.id),
                    job_type="map",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(other_map_job)
                db.session.commit()

                response = client.post(
                    f"/projects/{project_id}/flows/{flow_id}/sections/{section_id}/questions/new",
                    data={
                        "question_code": "Q3",
                        "question_text": "Question 3",
                        "question_type": "open",
                    },
                )
                db.session.expire_all()
                q3 = (
                    InterviewFlowQuestion.query
                    .join(InterviewFlowSection)
                    .filter(
                        InterviewFlowSection.flow_id == flow_id,
                        InterviewFlowQuestion.question_code == "Q3",
                    )
                    .first()
                )
                failures += check(
                    "mapping on a different flow does not overblock question mutation",
                    response.status_code == 302 and q3 is not None,
                    f"status={response.status_code}",
                )

                other_map_job = db.session.get(ProcessingJob, int(other_map_job.id))
                other_map_job.status = "succeeded"
                other_map_job.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                stale_map_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=interview_id,
                    job_type="map",
                    status="pending",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(stale_map_job)
                db.session.commit()
                stale_map_job_id = int(stale_map_job.id)

                response = client.post(
                    f"/projects/{project_id}/flows/{flow_id}/sections/{section_id}/questions/new",
                    data={
                        "question_code": "Q4",
                        "question_text": "Question 4",
                        "question_type": "open",
                    },
                )
                db.session.expire_all()
                stale_after = db.session.get(ProcessingJob, stale_map_job_id)
                q4 = (
                    InterviewFlowQuestion.query
                    .join(InterviewFlowSection)
                    .filter(
                        InterviewFlowSection.flow_id == flow_id,
                        InterviewFlowQuestion.question_code == "Q4",
                    )
                    .first()
                )
                failures += check(
                    "stale mapping job is recovered before flow-write decision",
                    response.status_code == 302
                    and stale_after.status == "failed"
                    and q4 is not None,
                    f"status={response.status_code} job={stale_after.to_dict()}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "flow input fencing smoke",
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
