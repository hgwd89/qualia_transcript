from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


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
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_canonical_media_") as tmp:
        db_path = Path(tmp) / "canonical-media.db"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        try:
            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile, Transcription
            from models.project import Project
            from services.media_source import canonical_media_for_interview
            from services.processing_jobs import _perform_transcription

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Canonical media selection")
                db.session.add(project)
                db.session.flush()
                interview = Interview(project_id=project.id, status="pending")
                db.session.add(interview)
                db.session.flush()

                now = datetime.now(timezone.utc)
                older_registration = MediaFile(
                    interview_id=interview.id,
                    original_filename="older-id.mp3",
                    stored_path=f"{project.id}/{interview.id}/older-id.mp3",
                    file_type="audio",
                    uploaded_at=now + timedelta(days=1),
                )
                db.session.add(older_registration)
                db.session.flush()
                newer_registration = MediaFile(
                    interview_id=interview.id,
                    original_filename="newer-id.mp3",
                    stored_path=f"{project.id}/{interview.id}/newer-id.mp3",
                    file_type="audio",
                    # Deliberately earlier timestamp: canonical ordering is the
                    # durable registration ID, not a nullable/mutable wall clock.
                    uploaded_at=now - timedelta(days=1),
                )
                db.session.add(newer_registration)
                db.session.flush()

                old_tr = Transcription(
                    media_file_id=older_registration.id,
                    status="done",
                    language="ja",
                )
                new_tr = Transcription(
                    media_file_id=newer_registration.id,
                    status="error",
                    language="ja",
                )
                db.session.add_all([old_tr, new_tr])
                db.session.commit()

                interview_id = int(interview.id)
                project_id = int(project.id)
                older_id = int(older_registration.id)
                newer_id = int(newer_registration.id)
                new_tr_id = int(new_tr.id)

                db.session.expire_all()
                interview = db.session.get(Interview, interview_id)
                relationship_ids = [int(row.id) for row in interview.media_files]
                failures += check(
                    "Interview.media_files has deterministic registration order",
                    relationship_ids == [older_id, newer_id],
                    f"ids={relationship_ids}",
                )

                selected = canonical_media_for_interview(interview_id)
                failures += check(
                    "canonical media selector chooses highest registration ID",
                    selected is not None and int(selected.id) == newer_id,
                    f"selected={getattr(selected, 'id', None)} newer={newer_id}",
                )

                response = client.get(f"/api/interviews/{interview_id}/status")
                payload = response.get_json() or {}
                failures += check(
                    "status endpoint reports transcription state for canonical media",
                    response.status_code == 200 and payload.get("transcription_status") == "error",
                    f"status={response.status_code} payload={payload}",
                )

                new_tr = db.session.get(Transcription, new_tr_id)
                new_tr.status = "done"
                db.session.commit()

                result = _perform_transcription(SimpleNamespace(
                    project_id=project_id,
                    interview_id=interview_id,
                ))
                failures += check(
                    "durable transcription worker reuses done result for same canonical media",
                    result.get("already_done") is True
                    and int(result.get("transcription_id")) == new_tr_id,
                    f"result={result}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "canonical media selection smoke",
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
