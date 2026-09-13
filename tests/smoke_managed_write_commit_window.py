import io
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def _cleanup_replaced_directory(parent: Path, saved: Path, decoy: Path) -> None:
    if decoy.exists():
        decoy.unlink()
    if parent.exists():
        parent.rmdir()
    if saved.exists():
        saved.rename(parent)


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

    with tempfile.TemporaryDirectory(prefix="qualia_managed_commit_window_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'commit-window.db').as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")

        try:
            from sqlalchemy import event
            from werkzeug.datastructures import FileStorage

            from app import create_app
            from models import db
            from models.generated_file import GeneratedFile
            from models.interview import Interview, MediaFile
            from models.project import Project
            import services.upload_manager as upload_manager
            from services.file_manager import (
                open_output_target_for_write,
                prepare_output_target,
                register_generated_file,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Managed write commit-window race")
                db.session.add(project)
                db.session.commit()
                project_id = int(project.id)

                # Generated output: acquire the production commit guard first, then
                # race the public project-directory pathname from SQLAlchemy's
                # before_commit hook. POSIX permits the rename despite pinned FDs;
                # Windows should reject it because the retained handles omit delete
                # sharing. Both outcomes must preserve namespace/DB alignment.
                target = prepare_output_target(project_id, "commit-window.xlsx")
                writer = open_output_target_for_write(target)
                writer.stream.write(b"original-output")
                writer.close()

                target_path = Path(target.full_path)
                target_parent = target_path.parent
                target_saved = target_parent.with_name(f"{target_parent.name}.commit-window-original")
                target_decoy = target_parent / target_path.name
                generated_race = {"ran": False, "renamed": False, "blocked": False}

                session = db.session()

                def race_generated_before_commit(_session):
                    generated_race["ran"] = True
                    try:
                        target_parent.rename(target_saved)
                    except OSError:
                        generated_race["blocked"] = True
                        return
                    generated_race["renamed"] = True
                    target_parent.mkdir()
                    target_decoy.write_bytes(b"replacement-output")

                before_generated = GeneratedFile.query.count()
                event.listen(session, "before_commit", race_generated_before_commit, once=True)
                generated_raised = False
                generated_file = None
                try:
                    generated_file = register_generated_file(
                        target,
                        project_id=project_id,
                        file_type="analysis",
                        file_format="xlsx",
                    )
                except ValueError as exc:
                    generated_raised = "namespace changed after write" in str(exc)
                finally:
                    if event.contains(session, "before_commit", race_generated_before_commit):
                        event.remove(session, "before_commit", race_generated_before_commit)

                if os.name == "nt":
                    failures += check(
                        "Windows generated-output commit guard blocks ancestor rename during DB commit",
                        generated_race["ran"]
                        and generated_race["blocked"]
                        and not generated_race["renamed"]
                        and not generated_raised
                        and generated_file is not None
                        and GeneratedFile.query.count() == before_generated + 1
                        and target_path.read_bytes() == b"original-output",
                        f"race={generated_race} raised={generated_raised}",
                    )
                else:
                    predecessor = target_saved / target_path.name
                    failures += check(
                        "POSIX generated-output commit-window replacement is detected and DB row compensated",
                        generated_race["ran"]
                        and generated_race["renamed"]
                        and generated_raised
                        and GeneratedFile.query.count() == before_generated
                        and target_decoy.read_bytes() == b"replacement-output",
                        f"race={generated_race} raised={generated_raised} count={GeneratedFile.query.count()}",
                    )
                    failures += check(
                        "POSIX generated-output rollback deletes only pinned predecessor generation",
                        target_decoy.is_file() and not predecessor.exists(),
                        f"decoy={target_decoy.exists()} predecessor={predecessor.exists()}",
                    )
                    _cleanup_replaced_directory(target_parent, target_saved, target_decoy)

                # Upload path: use an already-committed interview so the regression
                # isolates MediaFile/file atomicity rather than interview creation.
                interview = Interview(project_id=project_id, notes="commit-window upload")
                db.session.add(interview)
                db.session.commit()

                media_target = upload_manager.prepare_media_upload_target(
                    int(interview.id),
                    "commit-window.wav",
                )
                media_path = Path(media_target.full_path)
                media_parent = media_path.parent
                media_saved = media_parent.with_name(f"{media_parent.name}.commit-window-original")
                media_decoy = media_parent / media_path.name
                media_race = {"ran": False, "renamed": False, "blocked": False}

                def race_media_before_commit(_session):
                    media_race["ran"] = True
                    try:
                        media_parent.rename(media_saved)
                    except OSError:
                        media_race["blocked"] = True
                        return
                    media_race["renamed"] = True
                    media_parent.mkdir()
                    media_decoy.write_bytes(b"replacement-upload")

                storage = FileStorage(
                    stream=io.BytesIO(b"original-upload"),
                    filename="commit-window.wav",
                    content_type="audio/wav",
                )
                before_media = MediaFile.query.count()
                event.listen(session, "before_commit", race_media_before_commit, once=True)
                media_raised = False
                media = None
                try:
                    with patch.object(
                        upload_manager,
                        "prepare_media_upload_target",
                        return_value=media_target,
                    ):
                        media = upload_manager.save_and_register_media(
                            storage,
                            interview,
                            original_filename=storage.filename,
                            mime_type=storage.content_type,
                        )
                except ValueError as exc:
                    media_raised = "namespace changed after write" in str(exc)
                finally:
                    if event.contains(session, "before_commit", race_media_before_commit):
                        event.remove(session, "before_commit", race_media_before_commit)

                if os.name == "nt":
                    failures += check(
                        "Windows upload commit guard blocks ancestor rename during DB commit",
                        media_race["ran"]
                        and media_race["blocked"]
                        and not media_race["renamed"]
                        and not media_raised
                        and media is not None
                        and MediaFile.query.count() == before_media + 1
                        and media_path.read_bytes() == b"original-upload",
                        f"race={media_race} raised={media_raised}",
                    )
                else:
                    media_predecessor = media_saved / media_path.name
                    failures += check(
                        "POSIX upload commit-window replacement is detected and MediaFile row compensated",
                        media_race["ran"]
                        and media_race["renamed"]
                        and media_raised
                        and MediaFile.query.count() == before_media
                        and media_decoy.read_bytes() == b"replacement-upload",
                        f"race={media_race} raised={media_raised} count={MediaFile.query.count()}",
                    )
                    failures += check(
                        "POSIX upload rollback deletes only pinned predecessor generation",
                        media_decoy.is_file() and not media_predecessor.exists(),
                        f"decoy={media_decoy.exists()} predecessor={media_predecessor.exists()}",
                    )
                    _cleanup_replaced_directory(media_parent, media_saved, media_decoy)

                db.session.remove()
                db.engine.dispose()
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
