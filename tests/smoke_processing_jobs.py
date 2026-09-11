import sys
import tempfile
from datetime import datetime, timezone
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

    with tempfile.TemporaryDirectory(prefix="qualia_processing_jobs_") as tmp:
        tmp_dir = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(tmp_dir / 'jobs.db').as_posix()}"
        config.UPLOAD_DIR = str(tmp_dir / "uploads")
        config.OUTPUT_DIR = str(tmp_dir / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment
            from services.processing_jobs import create_or_get_active_job, execute_job
            import routes.analyze as analyze_routes
            import routes.transcribe as transcribe_routes

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Processing Job Smoke")
                db.session.add(project)
                db.session.flush()
                interview = Interview(project_id=project.id, status="pending")
                db.session.add(interview)
                db.session.flush()
                media = MediaFile(
                    interview_id=interview.id,
                    original_filename="smoke.wav",
                    stored_path="smoke.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(media)
                db.session.commit()
                project_id = project.id
                interview_id = interview.id

            old_transcribe_launcher = transcribe_routes.launch_job_worker
            old_analyze_launcher = analyze_routes.launch_job_worker
            transcribe_routes.launch_job_worker = lambda job_id: 4242
            analyze_routes.launch_job_worker = lambda job_id: 4343
            try:
                response = client.post(f"/api/interviews/{interview_id}/transcribe", json={})
                data = response.get_json() or {}
                failures += check(
                    "transcription request is queued",
                    response.status_code == 202 and data.get("queued") is True and bool(data.get("job_id")),
                    f"status={response.status_code} data={data}",
                )
                first_job_id = data.get("job_id")

                duplicate = client.post(f"/api/interviews/{interview_id}/transcribe", json={})
                duplicate_data = duplicate.get_json() or {}
                failures += check(
                    "duplicate active transcription reuses job",
                    duplicate.status_code == 202
                    and duplicate_data.get("job_id") == first_job_id
                    and duplicate_data.get("created") is False,
                    str(duplicate_data),
                )

                status_response = client.get(f"/api/processing-jobs/{first_job_id}")
                status_data = status_response.get_json() or {}
                failures += check(
                    "job status API exposes durable state",
                    status_response.status_code == 200
                    and status_data.get("job", {}).get("status") == "pending",
                    str(status_data),
                )

                with app.app_context():
                    interview = db.session.get(Interview, interview_id)
                    interview.status = "transcribed"
                    db.session.add(Segment(
                        interview_id=interview_id,
                        speaker_label="P01",
                        speaker_role="respondent",
                        text="smoke",
                        seq=1,
                    ))
                    db.session.commit()

                conflicting_map = client.post(f"/api/interviews/{interview_id}/map", json={})
                conflict_data = conflicting_map.get_json() or {}
                failures += check(
                    "different active job on same interview is rejected",
                    conflicting_map.status_code == 409
                    and conflict_data.get("conflicting_job", {}).get("id") == first_job_id,
                    str(conflict_data),
                )

                with app.app_context():
                    transcribe_job = db.session.get(ProcessingJob, first_job_id)
                    transcribe_job.status = "succeeded"
                    transcribe_job.finished_at = datetime.now(timezone.utc)
                    db.session.commit()

                    job, created = create_or_get_active_job(project_id, "map", interview_id)
                    map_job_id = job.id
                    failures += check("map job created after prior job completes", created is True)

                    def success_handler(job):
                        return {"mapped_count": 7, "job_id": job.id}

                    completed = execute_job(map_job_id, handlers={"map": success_handler})
                    failures += check(
                        "worker success is persisted",
                        completed.status == "succeeded"
                        and completed.attempt_count == 1
                        and completed.to_dict()["result"]["mapped_count"] == 7,
                        str(completed.to_dict()),
                    )

                    failed_job = ProcessingJob(
                        project_id=project_id,
                        interview_id=interview_id,
                        job_type="analyze",
                        status="pending",
                    )
                    db.session.add(failed_job)
                    db.session.commit()
                    failed_job_id = failed_job.id

                    def failure_handler(job):
                        raise RuntimeError("intentional smoke failure")

                    failed = execute_job(failed_job_id, handlers={"analyze": failure_handler})
                    failures += check(
                        "worker failure is persisted",
                        failed.status == "failed"
                        and "intentional smoke failure" in (failed.error_message or ""),
                        str(failed.to_dict()),
                    )

                retry = client.post(f"/api/processing-jobs/{failed_job_id}/retry", json={})
                retry_data = retry.get_json() or {}
                failures += check(
                    "failed job can be requeued",
                    retry.status_code == 202
                    and retry_data.get("job_id") == failed_job_id,
                    str(retry_data),
                )

                with app.app_context():
                    retried = db.session.get(ProcessingJob, failed_job_id)
                    failures += check(
                        "retry resets durable status",
                        retried.status == "pending"
                        and retried.error_message is None
                        and retried.attempt_count == 1,
                        str(retried.to_dict()),
                    )
                    interview = db.session.get(Interview, interview_id)
                    interview.status = "mapped"
                    db.session.commit()

                analyze_response = client.post(f"/api/interviews/{interview_id}/analyze", json={})
                analyze_data = analyze_response.get_json() or {}
                failures += check(
                    "analysis request reuses active retried job",
                    analyze_response.status_code == 202
                    and analyze_data.get("queued") is True
                    and analyze_data.get("job_id") == failed_job_id
                    and analyze_data.get("created") is False,
                    str(analyze_data),
                )

                detail_response = client.get(f"/interviews/{interview_id}")
                failures += check(
                    "interview page loads durable job UI",
                    detail_response.status_code == 200
                    and b"processing_jobs.js" in detail_response.data,
                    f"status={detail_response.status_code}",
                )
            finally:
                transcribe_routes.launch_job_worker = old_transcribe_launcher
                analyze_routes.launch_job_worker = old_analyze_launcher

            with app.app_context():
                failures += check(
                    "processing_jobs table exists",
                    ProcessingJob.query.count() >= 3,
                    f"count={ProcessingJob.query.count()}",
                )
                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check("processing job smoke", False, f"{type(exc).__name__}: {exc}")
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
