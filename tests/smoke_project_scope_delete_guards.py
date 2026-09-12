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

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    with tempfile.TemporaryDirectory(prefix="qualia_scope_guard_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'scope.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        try:
            from app import create_app
            from models import db
            from models.project import Project
            from models.participant import Participant
            from models.interview import Interview
            from models.processing_job import ProcessingJob
            from models.interview_flow import InterviewFlow, InterviewFlowSection, InterviewFlowQuestion

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                p1 = Project(name="P1")
                p2 = Project(name="P2")
                db.session.add_all([p1, p2])
                db.session.flush()
                p1_id, p2_id = int(p1.id), int(p2.id)

                used_participant = Participant(
                    project_id=p1_id,
                    participant_code="P01",
                    display_name="Used",
                )
                unused_participant = Participant(
                    project_id=p1_id,
                    participant_code="P02",
                    display_name="Unused",
                )
                db.session.add_all([used_participant, unused_participant])
                db.session.flush()
                used_participant_id = int(used_participant.id)
                unused_participant_id = int(unused_participant.id)

                used_flow = InterviewFlow(project_id=p1_id, title="Used flow")
                job_flow = InterviewFlow(project_id=p1_id, title="Job flow")
                unused_flow = InterviewFlow(project_id=p1_id, title="Unused flow")
                other_flow = InterviewFlow(project_id=p1_id, title="Other flow")
                db.session.add_all([used_flow, job_flow, unused_flow, other_flow])
                db.session.flush()
                used_flow_id = int(used_flow.id)
                job_flow_id = int(job_flow.id)
                unused_flow_id = int(unused_flow.id)
                other_flow_id = int(other_flow.id)

                section = InterviewFlowSection(flow_id=used_flow_id, title="Used section", seq=1)
                job_section = InterviewFlowSection(flow_id=job_flow_id, title="Job section", seq=1)
                other_section = InterviewFlowSection(flow_id=other_flow_id, title="Other section", seq=1)
                db.session.add_all([section, job_section, other_section])
                db.session.flush()
                section_id = int(section.id)
                job_section_id = int(job_section.id)
                other_section_id = int(other_section.id)

                db.session.add(InterviewFlowQuestion(
                    section_id=section_id,
                    question_code="Q1",
                    question_text="Question",
                    seq=1,
                ))
                job_question = InterviewFlowQuestion(
                    section_id=job_section_id,
                    question_code="QJ1",
                    question_text="Queued analysis question",
                    seq=1,
                )
                db.session.add(job_question)
                db.session.flush()

                db.session.add(ProcessingJob(
                    project_id=p1_id,
                    question_id=int(job_question.id),
                    job_type="analyze_question",
                    status="succeeded",
                ))

                interview = Interview(
                    project_id=p1_id,
                    participant_id=used_participant_id,
                    flow_id=used_flow_id,
                )
                db.session.add(interview)
                db.session.commit()

                client = app.test_client()

                failures += check(
                    "participant edit enforces project boundary",
                    client.get(
                        f"/projects/{p2_id}/participants/{used_participant_id}/edit"
                    ).status_code == 404,
                )
                failures += check(
                    "participant delete enforces project boundary",
                    client.post(
                        f"/projects/{p2_id}/participants/{used_participant_id}/delete"
                    ).status_code == 404,
                )
                failures += check(
                    "flow detail enforces project boundary",
                    client.get(
                        f"/projects/{p2_id}/flows/{used_flow_id}"
                    ).status_code == 404,
                )
                failures += check(
                    "flow section creation enforces project boundary",
                    client.post(
                        f"/projects/{p2_id}/flows/{used_flow_id}/sections/new",
                        data={"title": "bad"},
                    ).status_code == 404,
                )
                failures += check(
                    "question creation enforces section ownership",
                    client.post(
                        f"/projects/{p1_id}/flows/{used_flow_id}/sections/{other_section_id}/questions/new",
                        data={"question_text": "bad"},
                    ).status_code == 404,
                )
                failures += check(
                    "flow delete enforces project boundary",
                    client.post(
                        f"/projects/{p2_id}/flows/{used_flow_id}/delete"
                    ).status_code == 404,
                )

                participant_delete = client.post(
                    f"/projects/{p1_id}/participants/{used_participant_id}/delete"
                )
                failures += check(
                    "used participant deletion is blocked",
                    participant_delete.status_code == 302
                    and db.session.get(Participant, used_participant_id) is not None,
                )

                flow_delete = client.post(
                    f"/projects/{p1_id}/flows/{used_flow_id}/delete"
                )
                failures += check(
                    "used flow deletion is blocked",
                    flow_delete.status_code == 302
                    and db.session.get(InterviewFlow, used_flow_id) is not None,
                )

                job_flow_delete = client.post(
                    f"/projects/{p1_id}/flows/{job_flow_id}/delete"
                )
                failures += check(
                    "flow referenced only by terminal processing job is blocked",
                    job_flow_delete.status_code == 302
                    and db.session.get(InterviewFlow, job_flow_id) is not None,
                )

                unused_participant_delete = client.post(
                    f"/projects/{p1_id}/participants/{unused_participant_id}/delete"
                )
                failures += check(
                    "unused participant can be deleted",
                    unused_participant_delete.status_code == 302
                    and db.session.get(Participant, unused_participant_id) is None,
                )

                unused_flow_delete = client.post(
                    f"/projects/{p1_id}/flows/{unused_flow_id}/delete"
                )
                failures += check(
                    "unused flow can be deleted",
                    unused_flow_delete.status_code == 302
                    and db.session.get(InterviewFlow, unused_flow_id) is None,
                )

                db.session.remove()
                db.engine.dispose()
        finally:
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
