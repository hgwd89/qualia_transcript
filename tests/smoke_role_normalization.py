import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    failures = 0
    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_role_normalization_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'roles.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        app = None
        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.participant import Participant
            from models.project import Project
            from models.segment import Segment
            from services.transcription import auto_assign_speaker_roles

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Role normalization smoke")
                db.session.add(project)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Participant 1",
                )
                db.session.add(participant)
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    status="transcribed",
                )
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    seq=1,
                    speaker_label="SPEAKER_00",
                    speaker_role="moderator",
                    text="質問です。",
                )
                db.session.add(segment)

                auto_interview = Interview(project_id=project.id, status="transcribed")
                db.session.add(auto_interview)
                db.session.flush()
                db.session.add_all([
                    Segment(
                        interview_id=auto_interview.id,
                        seq=1,
                        speaker_label="SPEAKER_SHORT",
                        speaker_role="unknown",
                        text="短い",
                    ),
                    Segment(
                        interview_id=auto_interview.id,
                        seq=2,
                        speaker_label="SPEAKER_LONG",
                        speaker_role="unknown",
                        text="これは 長い 回答 です",
                    ),
                ])
                db.session.commit()
                interview_id = interview.id
                segment_id = segment.id
                participant_id = participant.id
                auto_interview_id = auto_interview.id

                auto_assign_speaker_roles(auto_interview_id)
                auto_segments = (
                    Segment.query
                    .filter_by(interview_id=auto_interview_id)
                    .order_by(Segment.seq.asc())
                    .all()
                )
                auto_roles = {seg.speaker_label: seg.speaker_role for seg in auto_segments}
                failures += check(
                    "auto assignment stores canonical moderator role",
                    auto_roles == {
                        "SPEAKER_SHORT": "moderator",
                        "SPEAKER_LONG": "respondent",
                    }
                    and all(seg.speaker_role != "interviewer" for seg in auto_segments),
                    str(auto_roles),
                )

            status_response = client.get(f"/api/interviews/{interview_id}/status")
            status_data = status_response.get_json() or {}
            roles = status_data.get("segment_roles") or []
            failures += check(
                "status API exposes canonical moderator role",
                status_response.status_code == 200
                and len(roles) == 1
                and roles[0].get("speaker_role") == "moderator",
                str(status_data),
            )

            malformed = client.post(
                f"/interviews/{interview_id}/segments/{segment_id}/role",
                data="{",
                content_type="application/json",
            )
            with app.app_context():
                from models.segment import Segment
                current = db.session.get(Segment, segment_id)
                failures += check(
                    "malformed role JSON is rejected without mutation",
                    malformed.status_code == 400
                    and current.speaker_role == "moderator"
                    and current.participant_id == participant_id,
                    f"status={malformed.status_code} role={current.speaker_role} participant={current.participant_id}",
                )

            empty = client.post(
                f"/interviews/{interview_id}/segments/{segment_id}/role",
                json={},
            )
            with app.app_context():
                current = db.session.get(Segment, segment_id)
                failures += check(
                    "empty role JSON is rejected without mutation",
                    empty.status_code == 400
                    and current.speaker_role == "moderator"
                    and current.participant_id == participant_id,
                    f"status={empty.status_code} role={current.speaker_role} participant={current.participant_id}",
                )

            partial = client.post(
                f"/interviews/{interview_id}/segments/{segment_id}/role",
                json={"speaker_role": "respondent"},
            )
            partial_data = partial.get_json() or {}
            failures += check(
                "partial role update preserves existing participant",
                partial.status_code == 200
                and partial_data.get("speaker_role") == "respondent"
                and partial_data.get("participant_id") == participant_id,
                str(partial_data),
            )

            update_response = client.post(
                f"/interviews/{interview_id}/segments/{segment_id}/role",
                json={"speaker_role": "moderator", "participant_id": None},
            )
            update_data = update_response.get_json() or {}
            failures += check(
                "canonical moderator role is accepted by backend",
                update_response.status_code == 200
                and update_data.get("speaker_role") == "moderator"
                and update_data.get("participant_id") is None,
                str(update_data),
            )

            js = (repo_root / "static" / "js" / "processing_jobs.js").read_text(encoding="utf-8")
            failures += check(
                "legacy interviewer UI value is normalized to moderator",
                "raw === 'interviewer' ? 'moderator' : raw" in js,
            )
            failures += check(
                "moderator role is restored into legacy interviewer select",
                "if (value === 'moderator') value = 'interviewer';" in js,
            )
        except Exception as exc:
            failures += check("role normalization smoke", False, f"{type(exc).__name__}: {exc}")
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
