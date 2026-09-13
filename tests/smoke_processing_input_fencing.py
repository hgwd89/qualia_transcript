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

    with tempfile.TemporaryDirectory(prefix="qualia_input_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'input-fencing.db').as_posix()}"
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
            from models.segment import Segment, UtteranceMapping

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Input fencing smoke")
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Main flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Section", seq=1)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Question 1",
                    seq=1,
                )
                db.session.add(question)
                p1 = Participant(project_id=project.id, participant_code="P01")
                p2 = Participant(project_id=project.id, participant_code="P02")
                db.session.add_all([p1, p2])
                db.session.flush()
                i1 = Interview(
                    project_id=project.id,
                    participant_id=p1.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                i2 = Interview(
                    project_id=project.id,
                    participant_id=p2.id,
                    flow_id=flow.id,
                    status="mapped",
                )
                db.session.add_all([i1, i2])
                db.session.flush()
                s1 = Segment(
                    interview_id=i1.id,
                    participant_id=p1.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="first response",
                    seq=1,
                )
                s2 = Segment(
                    interview_id=i2.id,
                    participant_id=p2.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="second response",
                    seq=1,
                )
                db.session.add_all([s1, s2])
                db.session.flush()
                mapping = UtteranceMapping(
                    segment_id=s1.id,
                    question_id=question.id,
                    mapped_by="human",
                    confidence=1.0,
                    is_unclassified=False,
                )
                db.session.add(mapping)
                db.session.commit()

                project_id = int(project.id)
                i1_id = int(i1.id)
                i2_id = int(i2.id)
                s1_id = int(s1.id)
                s2_id = int(s2.id)
                q1_id = int(question.id)

                same_interview_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=i1_id,
                    job_type="analyze",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(same_interview_job)
                db.session.commit()
                same_job_id = int(same_interview_job.id)

                response = client.post(
                    f"/api/interviews/{i1_id}/segments/{s1_id}/mapping",
                    json={"question_id": None},
                )
                payload = response.get_json() or {}
                db.session.expire_all()
                mapping_after_block = UtteranceMapping.query.filter_by(segment_id=s1_id).first()
                failures += check(
                    "same-interview active job blocks manual mapping mutation",
                    response.status_code == 409
                    and same_job_id in (payload.get("active_job_ids") or [])
                    and mapping_after_block is not None
                    and mapping_after_block.question_id == q1_id,
                    f"status={response.status_code} payload={payload}",
                )

                same_interview_job = db.session.get(ProcessingJob, same_job_id)
                same_interview_job.status = "succeeded"
                same_interview_job.worker_pid = None
                same_interview_job.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                response = client.post(
                    f"/api/interviews/{i1_id}/segments/{s1_id}/mapping",
                    json={"question_id": None},
                )
                db.session.expire_all()
                mapping_after_terminal = UtteranceMapping.query.filter_by(segment_id=s1_id).first()
                failures += check(
                    "manual mapping mutation proceeds after conflicting job is terminal",
                    response.status_code == 200
                    and mapping_after_terminal is not None
                    and mapping_after_terminal.question_id is None
                    and mapping_after_terminal.is_unclassified is True,
                    f"status={response.status_code} body={response.get_json()}",
                )

                project_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=None,
                    question_id=None,
                    job_type="analyze_integrated",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(project_job)
                db.session.commit()
                project_job_id = int(project_job.id)

                response = client.post(
                    f"/interviews/{i2_id}/segments/{s2_id}/role",
                    json={"speaker_role": "observer"},
                )
                payload = response.get_json() or {}
                db.session.expire_all()
                role_after_block = db.session.get(Segment, s2_id)
                failures += check(
                    "project-wide active analysis blocks role mutation on any interview",
                    response.status_code == 409
                    and project_job_id in (payload.get("active_job_ids") or [])
                    and role_after_block.speaker_role == "respondent",
                    f"status={response.status_code} payload={payload}",
                )

                project_job = db.session.get(ProcessingJob, project_job_id)
                project_job.status = "succeeded"
                project_job.worker_pid = None
                project_job.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                response = client.post(
                    f"/interviews/{i2_id}/segments/{s2_id}/role",
                    json={"speaker_role": "observer"},
                )
                db.session.expire_all()
                role_after_terminal = db.session.get(Segment, s2_id)
                failures += check(
                    "role mutation proceeds after project-wide job is terminal",
                    response.status_code == 200
                    and role_after_terminal.speaker_role == "observer",
                    f"status={response.status_code} body={response.get_json()}",
                )

                stale_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=i1_id,
                    job_type="analyze",
                    status="pending",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                    progress_json='{"stage":"queued"}',
                )
                db.session.add(stale_job)
                db.session.commit()
                stale_job_id = int(stale_job.id)

                response = client.post(
                    f"/api/interviews/{i1_id}/segments/{s1_id}/mapping",
                    json={"question_id": q1_id},
                )
                db.session.expire_all()
                stale_after = db.session.get(ProcessingJob, stale_job_id)
                mapping_after_recovery = UtteranceMapping.query.filter_by(segment_id=s1_id).first()
                failures += check(
                    "stale no-worker job is recovered before input write decision",
                    response.status_code == 200
                    and stale_after.status == "failed"
                    and mapping_after_recovery is not None
                    and mapping_after_recovery.question_id == q1_id,
                    f"status={response.status_code} job={stale_after.to_dict()}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "processing input fencing smoke",
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
