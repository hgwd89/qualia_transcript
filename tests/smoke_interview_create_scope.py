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

    with tempfile.TemporaryDirectory(prefix="qualia_interview_scope_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'scope.db').as_posix()}"
        try:
            from app import create_app
            from models import db
            from models.project import Project
            from models.participant import Participant
            from models.interview import Interview
            from models.interview_flow import InterviewFlow

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                p1 = Project(name="P1")
                p2 = Project(name="P2")
                db.session.add_all([p1, p2])
                db.session.flush()

                local_participant = Participant(
                    project_id=p1.id,
                    participant_code="P01",
                )
                foreign_participant = Participant(
                    project_id=p2.id,
                    participant_code="P01",
                )
                local_flow = InterviewFlow(project_id=p1.id, title="Local")
                foreign_flow = InterviewFlow(project_id=p2.id, title="Foreign")
                db.session.add_all([
                    local_participant,
                    foreign_participant,
                    local_flow,
                    foreign_flow,
                ])
                db.session.commit()

                p1_id = int(p1.id)
                local_participant_id = int(local_participant.id)
                foreign_participant_id = int(foreign_participant.id)
                local_flow_id = int(local_flow.id)
                foreign_flow_id = int(foreign_flow.id)

                client = app.test_client()
                before = Interview.query.count()

                response = client.post(
                    f"/projects/{p1_id}/interviews/new",
                    data={
                        "participant_id": foreign_participant_id,
                        "flow_id": local_flow_id,
                    },
                )
                failures += check(
                    "interview create rejects foreign participant",
                    response.status_code == 404 and Interview.query.count() == before,
                    f"status={response.status_code}",
                )

                response = client.post(
                    f"/projects/{p1_id}/interviews/new",
                    data={
                        "participant_id": local_participant_id,
                        "flow_id": foreign_flow_id,
                    },
                )
                failures += check(
                    "interview create rejects foreign flow",
                    response.status_code == 404 and Interview.query.count() == before,
                    f"status={response.status_code}",
                )

                response = client.post(
                    f"/projects/{p1_id}/interviews/new",
                    data={
                        "participant_id": local_participant_id,
                        "flow_id": local_flow_id,
                    },
                )
                created = Interview.query.order_by(Interview.id.desc()).first()
                failures += check(
                    "interview create accepts same-project references",
                    response.status_code == 302
                    and Interview.query.count() == before + 1
                    and created is not None
                    and created.project_id == p1_id
                    and created.participant_id == local_participant_id
                    and created.flow_id == local_flow_id,
                    f"status={response.status_code}",
                )

                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original_uri

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
