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
            from models.project import Project
            from models.segment import Segment

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Role normalization smoke")
                db.session.add(project)
                db.session.flush()
                interview = Interview(project_id=project.id, status="transcribed")
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    seq=1,
                    speaker_label="SPEAKER_00",
                    speaker_role="moderator",
                    text="質問です。",
                )
                db.session.add(segment)
                db.session.commit()
                interview_id = interview.id
                segment_id = segment.id

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

            update_response = client.post(
                f"/interviews/{interview_id}/segments/{segment_id}/role",
                json={"speaker_role": "moderator", "participant_id": None},
            )
            update_data = update_response.get_json() or {}
            failures += check(
                "canonical moderator role is accepted by backend",
                update_response.status_code == 200
                and update_data.get("speaker_role") == "moderator",
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
