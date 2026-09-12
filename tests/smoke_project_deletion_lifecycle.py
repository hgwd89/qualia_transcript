import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


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
        "BASE_DIR": config.BASE_DIR,
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_project_delete_") as tmp:
        root = Path(tmp)
        db_path = root / "delete.db"
        config.BASE_DIR = str(root)
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")

        try:
            from sqlalchemy import event

            from app import create_app
            from models import db
            from models.generated_file import GeneratedFile
            from models.interview import Interview, MediaFile, Transcription
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services import project_deletion
            from services.project_deletion import (
                ProjectDeletionBlocked,
                ProjectDeletionStorageBlocked,
                delete_project,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Deletion success")
                db.session.add(project)
                db.session.flush()
                project_id = int(project.id)

                interview = Interview(project_id=project_id)
                db.session.add(interview)
                db.session.flush()
                interview_id = int(interview.id)

                media = MediaFile(
                    interview_id=interview_id,
                    original_filename="source.wav",
                    stored_path=f"{interview_id}/source.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(media)
                db.session.flush()
                media_id = int(media.id)

                transcription = Transcription(
                    media_file_id=media_id,
                    whisper_model="test",
                    language="ja",
                    status="done",
                )
                db.session.add(transcription)
                db.session.flush()
                transcription_id = int(transcription.id)

                generated = GeneratedFile(
                    project_id=project_id,
                    interview_id=interview_id,
                    file_type="analysis",
                    file_format="txt",
                    original_filename="result.txt",
                    stored_path=f"{project_id}/result.txt",
                )
                db.session.add(generated)
                db.session.flush()
                generated_id = int(generated.id)

                job = ProcessingJob(
                    project_id=project_id,
                    interview_id=interview_id,
                    job_type="analyze",
                    status="succeeded",
                )
                db.session.add(job)
                db.session.commit()
                job_id = int(job.id)

                project_output_dir = Path(config.OUTPUT_DIR) / str(project_id)
                project_output_dir.mkdir(parents=True, exist_ok=True)
                (project_output_dir / "result.txt").write_text("generated", encoding="utf-8")

                upload_dir = Path(config.UPLOAD_DIR) / str(interview_id)
                upload_dir.mkdir(parents=True, exist_ok=True)
                (upload_dir / "source.wav").write_bytes(b"media")

                raw_dir = Path(config.OUTPUT_DIR) / "raw_transcripts"
                raw_dir.mkdir(parents=True, exist_ok=True)
                raw_snapshot = raw_dir / f"transcription_{transcription_id}_20260912T000000Z.json"
                raw_manifest = raw_dir / f"transcription_{transcription_id}_manifest_20260912T000000Z.json"
                raw_snapshot.write_text("{}", encoding="utf-8")
                raw_manifest.write_text("{}", encoding="utf-8")

                logs_dir = Path(config.BASE_DIR) / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                job_log = logs_dir / f"processing_job_{job_id}.log"
                job_log.write_text("worker log", encoding="utf-8")

                original_build = project_deletion._build_storage_plan
                lock_seen = {"value": False}

                def probe_lock(current_project, job_ids):
                    probe = sqlite3.connect(str(db_path), timeout=0)
                    try:
                        try:
                            probe.execute("BEGIN IMMEDIATE")
                        except sqlite3.OperationalError:
                            lock_seen["value"] = True
                        else:
                            probe.rollback()
                    finally:
                        probe.close()
                    return original_build(current_project, job_ids)

                with patch(
                    "services.project_deletion._build_storage_plan",
                    side_effect=probe_lock,
                ):
                    result = delete_project(project)

                failures += check(
                    "project deletion serializes against competing writes",
                    lock_seen["value"],
                )
                failures += check(
                    "project deletion removes DB graph",
                    db.session.get(Project, project_id) is None
                    and db.session.get(Interview, interview_id) is None
                    and db.session.get(MediaFile, media_id) is None
                    and db.session.get(Transcription, transcription_id) is None
                    and db.session.get(GeneratedFile, generated_id) is None
                    and db.session.get(ProcessingJob, job_id) is None,
                )
                failures += check(
                    "project deletion preserves raw transcript snapshots",
                    raw_snapshot.is_file() and raw_manifest.is_file(),
                )
                failures += check(
                    "project deletion cleans quarantined managed storage",
                    not project_output_dir.exists()
                    and not upload_dir.exists()
                    and not job_log.exists()
                    and not result.cleanup_errors
                    and not list(Path(config.OUTPUT_DIR).glob(".qualia-delete-quarantine-*"))
                    and not list(Path(config.UPLOAD_DIR).glob(".qualia-delete-quarantine-*"))
                    and not list(logs_dir.glob(".qualia-delete-quarantine-*")),
                    f"removed_paths={result.removed_paths} errors={result.cleanup_errors}",
                )

                errors: list[str] = []
                with patch(
                    "services.project_deletion._is_link_or_reparse",
                    return_value=True,
                ), patch(
                    "services.project_deletion._remove_link_only",
                    return_value=1,
                ) as remove_link:
                    removed = project_deletion._remove_dir(root / "linked-entry", errors)
                failures += check(
                    "linked managed path uses non-recursive cleanup",
                    removed == 1 and remove_link.called and not errors,
                )

                blocked = Project(name="Deletion blocked")
                db.session.add(blocked)
                db.session.flush()
                blocked_id = int(blocked.id)
                active = ProcessingJob(
                    project_id=blocked_id,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(active)
                db.session.commit()
                active_id = int(active.id)

                blocked_raised = False
                try:
                    delete_project(blocked)
                except ProjectDeletionBlocked as exc:
                    blocked_raised = active_id in exc.active_job_ids

                failures += check(
                    "active durable job blocks project deletion",
                    blocked_raised
                    and db.session.get(Project, blocked_id) is not None
                    and db.session.get(ProcessingJob, active_id) is not None,
                )

                quarantine_blocked = Project(name="Quarantine blocked")
                db.session.add(quarantine_blocked)
                db.session.commit()
                quarantine_blocked_id = int(quarantine_blocked.id)
                blocked_dir = Path(config.OUTPUT_DIR) / str(quarantine_blocked_id)
                blocked_dir.mkdir(parents=True, exist_ok=True)
                blocked_marker = blocked_dir / "must-stay.txt"
                blocked_marker.write_text("keep", encoding="utf-8")

                storage_blocked = False
                with patch(
                    "services.project_deletion.os.replace",
                    side_effect=PermissionError("simulated quarantine rename failure"),
                ):
                    try:
                        delete_project(quarantine_blocked)
                    except ProjectDeletionStorageBlocked:
                        storage_blocked = True

                failures += check(
                    "quarantine failure blocks DB deletion and preserves numeric path",
                    storage_blocked
                    and db.session.get(Project, quarantine_blocked_id) is not None
                    and blocked_marker.is_file(),
                )

                commit_fail_project = Project(name="Commit failure")
                db.session.add(commit_fail_project)
                db.session.commit()
                commit_fail_id = int(commit_fail_project.id)
                marker_dir = Path(config.OUTPUT_DIR) / str(commit_fail_id)
                marker_dir.mkdir(parents=True, exist_ok=True)
                marker = marker_dir / "must-survive.txt"
                marker.write_text("keep", encoding="utf-8")

                session = db.session()

                def fail_before_commit(_session):
                    raise RuntimeError("simulated deletion commit failure")

                event.listen(session, "before_commit", fail_before_commit, once=True)
                commit_failed = False
                try:
                    delete_project(commit_fail_project)
                except RuntimeError:
                    commit_failed = True
                finally:
                    if event.contains(session, "before_commit", fail_before_commit):
                        event.remove(session, "before_commit", fail_before_commit)

                failures += check(
                    "DB commit failure restores quarantined project storage",
                    commit_failed
                    and db.session.get(Project, commit_fail_id) is not None
                    and marker.is_file()
                    and not list(Path(config.OUTPUT_DIR).glob(
                        f".qualia-delete-quarantine-{commit_fail_id}-*"
                    )),
                )

                cleanup_fail_project = Project(name="Cleanup failure")
                db.session.add(cleanup_fail_project)
                db.session.flush()
                cleanup_fail_id = int(cleanup_fail_project.id)
                cleanup_fail_job = ProcessingJob(
                    project_id=cleanup_fail_id,
                    job_type="analyze",
                    status="succeeded",
                )
                db.session.add(cleanup_fail_job)
                db.session.commit()
                cleanup_fail_job_id = int(cleanup_fail_job.id)

                cleanup_fail_dir = Path(config.OUTPUT_DIR) / str(cleanup_fail_id)
                cleanup_fail_dir.mkdir(parents=True, exist_ok=True)
                old_marker = cleanup_fail_dir / "old-owner.txt"
                old_marker.write_text("old", encoding="utf-8")
                old_job_log = logs_dir / f"processing_job_{cleanup_fail_job_id}.log"
                old_job_log.write_text("old-job", encoding="utf-8")

                def fail_quarantine_cleanup(entry, cleanup_errors):
                    cleanup_errors.append(
                        f"simulated quarantine cleanup failure: {entry.quarantine_path}"
                    )
                    return 0

                with patch(
                    "services.project_deletion._remove_quarantined_entry",
                    side_effect=fail_quarantine_cleanup,
                ):
                    cleanup_fail_result = delete_project(cleanup_fail_project)

                quarantines = list(Path(config.OUTPUT_DIR).glob(
                    f".qualia-delete-quarantine-{cleanup_fail_id}-*"
                ))
                log_quarantines = list(logs_dir.glob(
                    f".qualia-delete-quarantine-processing_job_{cleanup_fail_job_id}.log-*"
                ))
                failures += check(
                    "post-commit cleanup failure leaves only nonreusable quarantine names",
                    db.session.get(Project, cleanup_fail_id) is None
                    and db.session.get(ProcessingJob, cleanup_fail_job_id) is None
                    and not cleanup_fail_dir.exists()
                    and not old_job_log.exists()
                    and bool(cleanup_fail_result.cleanup_errors)
                    and len(quarantines) == 1
                    and len(log_quarantines) == 1
                    and (quarantines[0] / "old-owner.txt").is_file()
                    and log_quarantines[0].read_text(encoding="utf-8") == "old-job",
                    (
                        f"errors={cleanup_fail_result.cleanup_errors} "
                        f"storage_quarantines={quarantines} log_quarantines={log_quarantines}"
                    ),
                )

                replacement = Project(name="Replacement after cleanup failure")
                db.session.add(replacement)
                db.session.flush()
                replacement_id = int(replacement.id)
                replacement_job = ProcessingJob(
                    project_id=replacement_id,
                    job_type="analyze",
                    status="succeeded",
                )
                db.session.add(replacement_job)
                db.session.commit()
                replacement_job_id = int(replacement_job.id)

                replacement_dir = Path(config.OUTPUT_DIR) / str(replacement_id)
                replacement_dir.mkdir(parents=True, exist_ok=True)
                replacement_marker = replacement_dir / "new-owner.txt"
                replacement_marker.write_text("new", encoding="utf-8")
                replacement_job_log = logs_dir / f"processing_job_{replacement_job_id}.log"
                replacement_job_log.write_text("new-job", encoding="utf-8")

                failures += check(
                    "reused project/job IDs cannot inherit or lose predecessor storage",
                    replacement_id == cleanup_fail_id
                    and replacement_job_id == cleanup_fail_job_id
                    and replacement_marker.is_file()
                    and not (replacement_dir / "old-owner.txt").exists()
                    and replacement_job_log.read_text(encoding="utf-8") == "new-job"
                    and len(quarantines) == 1
                    and len(log_quarantines) == 1
                    and (quarantines[0] / "old-owner.txt").is_file()
                    and log_quarantines[0].read_text(encoding="utf-8") == "old-job",
                    (
                        f"project={cleanup_fail_id}->{replacement_id} "
                        f"job={cleanup_fail_job_id}->{replacement_job_id}"
                    ),
                )

                db.session.remove()
                db.engine.dispose()

        finally:
            config.BASE_DIR = original["BASE_DIR"]
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
