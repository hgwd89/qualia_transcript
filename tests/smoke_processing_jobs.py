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
            from services.processing_jobs import (
                _claim_pending_job,
                _record_worker_launch,
                _release_worker_launch_reservation,
                _reserve_worker_launch,
                create_or_get_active_job,
                execute_job,
                retry_failed_job,
            )
            import routes.analyze as analyze_routes
            import routes.transcribe as transcribe_routes

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()

            with app.app_context():
                project = Project(name="Processing Job Smoke")
                atomic_project = Project(name="Atomic Job Smoke")
                db.session.add_all([project, atomic_project])
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
                atomic_project_id = atomic_project.id
                interview_id = interview.id

                # Atomic pending -> running claim: a second worker cannot execute
                # the same durable job after the first worker has claimed it.
                atomic_job = ProcessingJob(
                    project_id=atomic_project_id,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(atomic_job)
                db.session.commit()
                atomic_job_id = atomic_job.id

                first_claim, first_claimed = _claim_pending_job(atomic_job_id, worker_pid=7777)
                second_claim, second_claimed = _claim_pending_job(atomic_job_id, worker_pid=8888)
                failures += check(
                    "pending job is atomically claimed only once",
                    first_claimed is True
                    and second_claimed is False
                    and second_claim.status == "running"
                    and second_claim.attempt_count == 1
                    and second_claim.worker_pid == 7777,
                    str(second_claim.to_dict()),
                )
                second_claim.status = "succeeded"
                second_claim.worker_pid = None
                second_claim.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                # Launch reservation is also conditional so concurrent launchers
                # cannot both spawn workers for the same active job.
                launch_job = ProcessingJob(
                    project_id=atomic_project_id,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(launch_job)
                db.session.commit()
                launch_job_id = launch_job.id

                reserved_job, reserved = _reserve_worker_launch(launch_job_id)
                duplicate_reservation_job, duplicate_reserved = _reserve_worker_launch(launch_job_id)
                failures += check(
                    "worker launch slot is reserved only once",
                    reserved is True
                    and duplicate_reserved is False
                    and reserved_job.worker_pid == 0
                    and duplicate_reservation_job.worker_pid == 0,
                    str(duplicate_reservation_job.to_dict()),
                )
                launch_attempt = int(reserved_job.attempt_count or 0)
                launch_started_at = reserved_job.started_at
                recorded = _record_worker_launch(
                    launch_job_id,
                    5555,
                    expected_attempt_count=launch_attempt,
                    expected_started_at=launch_started_at,
                )
                launch_after_record = db.session.get(ProcessingJob, launch_job_id)
                failures += check(
                    "reserved launcher persists worker pid",
                    recorded is True
                    and launch_after_record.worker_pid == 5555
                    and (launch_after_record.to_dict().get("progress") or {}).get("stage") == "worker_started",
                    str(launch_after_record.to_dict()),
                )
                launch_after_record.status = "succeeded"
                launch_after_record.worker_pid = None
                launch_after_record.finished_at = datetime.now(timezone.utc)
                launch_after_record.progress_json = '{"stage":"completed"}'
                db.session.commit()
                late_record = _record_worker_launch(
                    launch_job_id,
                    5555,
                    expected_attempt_count=launch_attempt,
                    expected_started_at=launch_started_at,
                )
                terminal_launch = db.session.get(ProcessingJob, launch_job_id)
                failures += check(
                    "late launcher write cannot resurrect terminal pid",
                    late_record is False
                    and terminal_launch.status == "succeeded"
                    and terminal_launch.worker_pid is None
                    and (terminal_launch.to_dict().get("progress") or {}).get("stage") == "completed",
                    str(terminal_launch.to_dict()),
                )

                # Child can claim before parent records Popen PID; the child's own
                # PID becomes authoritative and the parent's late write is ignored.
                child_first_job = ProcessingJob(
                    project_id=atomic_project_id,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(child_first_job)
                db.session.commit()
                child_first_id = child_first_job.id
                child_reserved_job, child_reserved = _reserve_worker_launch(child_first_id)
                child_attempt = int(child_reserved_job.attempt_count or 0)
                child_started_at = child_reserved_job.started_at
                child_claim, child_claimed = _claim_pending_job(
                    child_first_id,
                    worker_pid=6666,
                    expected_attempt_count=child_attempt,
                    expected_started_at=child_started_at,
                )
                parent_late_record = _record_worker_launch(
                    child_first_id,
                    6666,
                    expected_attempt_count=child_attempt,
                    expected_started_at=child_started_at,
                )
                child_after = db.session.get(ProcessingJob, child_first_id)
                failures += check(
                    "child claim wins safely before parent pid write",
                    child_reserved is True
                    and child_claimed is True
                    and child_claim.worker_pid == 6666
                    and parent_late_record is False
                    and child_after.worker_pid == 6666
                    and child_after.attempt_count == 1,
                    str(child_after.to_dict()),
                )
                child_after.status = "succeeded"
                child_after.worker_pid = None
                child_after.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                release_job = ProcessingJob(
                    project_id=atomic_project_id,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(release_job)
                db.session.commit()
                release_id = release_job.id
                release_reserved_job, release_reserved = _reserve_worker_launch(release_id)
                release_attempt = int(release_reserved_job.attempt_count or 0)
                release_started_at = release_reserved_job.started_at
                _release_worker_launch_reservation(
                    release_id,
                    expected_attempt_count=release_attempt,
                    expected_started_at=release_started_at,
                )
                released = db.session.get(ProcessingJob, release_id)
                failures += check(
                    "failed spawn releases launch reservation",
                    release_reserved is True
                    and released.status == "pending"
                    and released.worker_pid is None,
                    str(released.to_dict()),
                )
                released.status = "succeeded"
                released.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                retry_race = ProcessingJob(
                    project_id=atomic_project_id,
                    job_type="project_pipeline",
                    status="failed",
                    error_message="old failure",
                )
                db.session.add(retry_race)
                db.session.commit()
                retry_race_id = retry_race.id
                first_retry = retry_failed_job(retry_race)
                second_retry_rejected = False
                try:
                    retry_failed_job(first_retry)
                except ValueError:
                    second_retry_rejected = True
                failures += check(
                    "failed job can be atomically requeued only once",
                    first_retry.status == "pending"
                    and first_retry.started_at is not None
                    and second_retry_rejected,
                    str(first_retry.to_dict()),
                )
                first_retry.status = "succeeded"
                first_retry.finished_at = datetime.now(timezone.utc)
                db.session.commit()

            old_transcribe_launcher = transcribe_routes.launch_job_or_preserve_active
            old_analyze_launcher = analyze_routes.launch_job_or_preserve_active
            transcribe_routes.launch_job_or_preserve_active = lambda _job: (4242, None)
            analyze_routes.launch_job_or_preserve_active = lambda _job: (4343, None)
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

                with app.app_context():
                    legacy_conflict_rejected = False
                    try:
                        create_or_get_active_job(project_id, "map", interview_id)
                    except ValueError:
                        legacy_conflict_rejected = True
                    failures += check(
                        "legacy create helper delegates canonical conflict policy",
                        legacy_conflict_rejected,
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

                    handler_calls = {"count": 0}

                    def success_handler(job):
                        handler_calls["count"] += 1
                        return {"mapped_count": 7, "job_id": job.id}

                    completed = execute_job(map_job_id, handlers={"map": success_handler})
                    duplicate_execute = execute_job(map_job_id, handlers={"map": success_handler})
                    failures += check(
                        "worker success is persisted",
                        completed.status == "succeeded"
                        and completed.attempt_count == 1
                        and completed.to_dict()["result"]["mapped_count"] == 7,
                        str(completed.to_dict()),
                    )
                    failures += check(
                        "terminal job handler cannot execute twice",
                        duplicate_execute.status == "succeeded" and handler_calls["count"] == 1,
                        f"calls={handler_calls['count']}",
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
                transcribe_routes.launch_job_or_preserve_active = old_transcribe_launcher
                analyze_routes.launch_job_or_preserve_active = old_analyze_launcher

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
