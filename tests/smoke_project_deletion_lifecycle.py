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
        "BASE_DIR": config.BASE_DIR,
        "DATABASE_URI": config.DATABASE_URI,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "UPLOAD_DIR": config.UPLOAD_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_project_delete_") as tmp:
        root = Path(tmp)
        config.BASE_DIR = str(root)
        config.DATABASE_URI = f"sqlite:///{(root / 'delete.db').as_posix()}"
        config.OUTPUT_DIR = str(root / "outputs")
        config.UPLOAD_DIR = str(root / "uploads")

        try:
            from app import create_app
            from models import db
            from models.generated_file import GeneratedFile
            from models.interview import Interview, MediaFile, Transcription
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.project_deletion import ProjectDeletionBlocked, delete_project

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

                interview_upload_dir = Path(config.UPLOAD_DIR) / str(interview_id)
                interview_upload_dir.mkdir(parents=True, exist_ok=True)
                (interview_upload_dir / "source.wav").write_bytes(b"media")

                raw_root = Path(config.OUTPUT_DIR) / "raw_transcripts"
                raw_root.mkdir(parents=True, exist_ok=True)
                raw_snapshot = raw_root / f"transcription_{transcription_id}_20260912T000000Z.json"
                raw_manifest = raw_root / f"transcription_{transcription_id}_manifest_20260912T000000Z.json"
                unrelated_raw = raw_root / "transcription_999999_20260912T000000Z.json"
                raw_snapshot.write_text("{}", encoding="utf-8")
                raw_manifest.write_text("{}", encoding="utf-8")
                unrelated_raw.write_text("{}", encoding="utf-8")

                logs_root = Path(config.BASE_DIR) / "logs"
                logs_root.mkdir(parents=True, exist_ok=True)
                job_log = logs_root / f"processing_job_{job_id}.log"
                job_log.write_text("worker log", encoding="utf-8")

                result = delete_project(project)
                failures += check(
                    "project deletion removes DB-owned project graph",
                    db.session.get(Project, project_id) is None
                    and db.session.get(Interview, interview_id) is None
                    and db.session.get(MediaFile, media_id) is None
                    and db.session.get(Transcription, transcription_id) is None
                    and db.session.get(GeneratedFile, generated_id) is None
                    and db.session.get(ProcessingJob, job_id) is None,
                )
                failures += check(
                    "project deletion removes managed storage",
                    not project_output_dir.exists()
                    and not interview_upload_dir.exists()
                    and not raw_snapshot.exists()
                    and not raw_manifest.exists()
                    and not job_log.exists()
                    and unrelated_raw.is_file()
                    and not result.cleanup_errors,
                    f"removed_paths={result.removed_paths} errors={result.cleanup_errors}",
                )

                blocked = Project(name="Deletion blocked")
                db.session.add(blocked)
                db.session.flush()
                blocked_id = int(blocked.id)
                active_job = ProcessingJob(
                    project_id=blocked_id,
                    interview_id=None,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(active_job)
                db.session.commit()
                active_job_id = int(active_job.id)

                blocked_output = Path(config.OUTPUT_DIR) / str(blocked_id)
                blocked_output.mkdir(parents=True, exist_ok=True)
                marker = blocked_output / "must-remain.txt"
                marker.write_text("keep", encoding="utf-8")

                blocked_raised = False
                blocked_ids = []
                try:
                    delete_project(blocked)
                except ProjectDeletionBlocked as exc:
                    blocked_raised = True
                    blocked_ids = exc.active_job_ids

                failures += check(
                    "active durable job blocks project deletion",
                    blocked_raised
                    and active_job_id in blocked_ids
                    and db.session.get(Project, blocked_id) is not None
                    and db.session.get(ProcessingJob, active_job_id) is not None
                    and marker.is_file(),
                    f"blocked_ids={blocked_ids}",
                )

                db.session.remove()
                db.engine.dispose()

            route_source = (repo_root / "routes" / "projects.py").read_text(encoding="utf-8")
            failures += check(
                "project delete route uses guarded deletion service",
                "delete_project(project)" in route_source
                and "ProjectDeletionBlocked" in route_source,
            )

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
