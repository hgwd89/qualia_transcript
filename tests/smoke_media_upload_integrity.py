import io
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def upload_files(upload_root: Path) -> set[str]:
    if not upload_root.exists():
        return set()
    return {
        str(path.relative_to(upload_root)).replace("\\", "/")
        for path in upload_root.rglob("*")
        if path.is_file()
    }


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_media_upload_") as tmp:
        root = Path(tmp)
        upload_root = root / "uploads"
        config.DATABASE_URI = f"sqlite:///{(root / 'media.db').as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(upload_root)

        try:
            from sqlalchemy import event
            from werkzeug.datastructures import FileStorage

            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile
            from models.project import Project
            from services.upload_manager import (
                get_media_full_path,
                media_extension,
                media_file_exists,
                save_and_register_media,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Media integrity")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                interview = Interview(project_id=project_id, notes="success")
                db.session.add(interview)
                db.session.flush()
                storage = FileStorage(
                    stream=io.BytesIO(b"fake-media-bytes"),
                    filename="sample.mp3",
                    content_type="audio/mpeg",
                )
                media = save_and_register_media(
                    storage,
                    interview,
                    original_filename=storage.filename,
                    mime_type=storage.content_type,
                )
                media_path = Path(get_media_full_path(media))
                failures += check(
                    "successful media upload keeps file and DB row aligned",
                    media.id is not None
                    and media.interview_id == interview.id
                    and media_file_exists(media)
                    and media_path.read_bytes() == b"fake-media-bytes"
                    and media.file_size_bytes == len(b"fake-media-bytes")
                    and "\\" not in media.stored_path,
                    f"media_id={media.id} stored_path={media.stored_path}",
                )
                failures += check(
                    "Japanese media filename preserves allowed extension",
                    media_extension("インタビュー音声.MP3") == ".mp3",
                )

                unsupported_rejected = False
                try:
                    media_extension("payload.exe")
                except ValueError:
                    unsupported_rejected = True
                failures += check(
                    "unsupported upload extension is rejected",
                    unsupported_rejected,
                )

                outside = root / "outside.mp3"
                outside.write_bytes(b"must-survive")
                malicious = MediaFile(
                    interview_id=int(interview.id),
                    original_filename="outside.mp3",
                    stored_path="../outside.mp3",
                    file_type="audio",
                    mime_type="audio/mpeg",
                )
                db.session.add(malicious)
                db.session.commit()
                rejected_escape = False
                try:
                    get_media_full_path(malicious)
                except ValueError:
                    rejected_escape = True
                failures += check(
                    "malicious media stored_path cannot escape UPLOAD_DIR",
                    rejected_escape and not media_file_exists(malicious) and outside.is_file(),
                )

                before_interviews = Interview.query.count()
                before_media = MediaFile.query.count()
                before_files = upload_files(upload_root)

                pending = Interview(project_id=project_id, notes="commit-failure")
                db.session.add(pending)
                db.session.flush()
                failed_interview_id = int(pending.id)
                failed_storage = FileStorage(
                    stream=io.BytesIO(b"orphan-candidate"),
                    filename="failure.wav",
                    content_type="audio/wav",
                )

                session = db.session()

                def fail_before_commit(_session):
                    raise RuntimeError("simulated media DB commit failure")

                event.listen(session, "before_commit", fail_before_commit, once=True)
                raised = False
                try:
                    save_and_register_media(
                        failed_storage,
                        pending,
                        original_filename=failed_storage.filename,
                        mime_type=failed_storage.content_type,
                    )
                except RuntimeError as exc:
                    raised = "simulated media DB commit failure" in str(exc)
                finally:
                    if event.contains(session, "before_commit", fail_before_commit):
                        event.remove(session, "before_commit", fail_before_commit)

                after_files = upload_files(upload_root)
                failures += check(
                    "media DB commit failure removes uploaded orphan and rolls back rows",
                    raised
                    and Interview.query.count() == before_interviews
                    and MediaFile.query.count() == before_media
                    and db.session.get(Interview, failed_interview_id) is None
                    and after_files == before_files,
                    (
                        f"raised={raised} interviews={Interview.query.count()}/{before_interviews} "
                        f"media={MediaFile.query.count()}/{before_media} files={after_files}"
                    ),
                )

                db.session.remove()
                db.engine.dispose()

            interviews_source = (repo_root / "routes" / "interviews.py").read_text(encoding="utf-8")
            transcription_source = (repo_root / "services" / "transcription.py").read_text(encoding="utf-8")
            failures += check(
                "interview upload route uses managed media registration",
                "save_and_register_media(" in interviews_source,
            )
            failures += check(
                "transcription resolves media through upload path guard",
                transcription_source.count("get_media_full_path(media)") == 2
                and "os.path.join(config.UPLOAD_DIR, media.stored_path)" not in transcription_source,
            )

        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
