import sys
import tempfile
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

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_job_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'fencing.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.processing_jobs import (
                JobLeaseLost,
                _claim_pending_job,
                _finish_job_failure,
                _finish_job_success,
                execute_job,
                retry_failed_job,
                update_progress,
            )

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Job fencing smoke")
                db.session.add(project)
                db.session.commit()
                project_id = project.id

                stale_success_job = ProcessingJob(
                    project_id=project_id,
                    job_type="map",
                    status="pending",
                )
                db.session.add(stale_success_job)
                db.session.commit()
                stale_success_id = stale_success_job.id
                takeover = {"claimed": False}

                def stale_success_handler(job):
                    current = db.session.get(ProcessingJob, job.id)
                    current.status = "failed"
                    current.worker_pid = None
                    current.error_message = "operator recovery"
                    db.session.commit()
                    retried = retry_failed_job(current)
                    second, claimed = _claim_pending_job(retried.id, worker_pid=2222)
                    takeover["claimed"] = claimed and second.attempt_count == 2
                    return {"stale_result": True}

                after_stale_success = execute_job(
                    stale_success_id,
                    handlers={"map": stale_success_handler},
                    worker_pid=1111,
                )
                failures += check(
                    "stale successful worker cannot finish newer attempt",
                    takeover["claimed"]
                    and after_stale_success.status == "running"
                    and after_stale_success.attempt_count == 2
                    and after_stale_success.worker_pid == 2222
                    and after_stale_success.result_json is None,
                    str(after_stale_success.to_dict()),
                )
                current, finished = _finish_job_success(stale_success_id, 2, {"fresh_result": True})
                failures += check(
                    "current attempt can still finish successfully",
                    finished is True
                    and current.status == "succeeded"
                    and (current.to_dict().get("result") or {}).get("fresh_result") is True,
                    str(current.to_dict()),
                )

                stale_failure_job = ProcessingJob(
                    project_id=project_id,
                    job_type="analyze",
                    status="pending",
                )
                db.session.add(stale_failure_job)
                db.session.commit()
                stale_failure_id = stale_failure_job.id
                failure_takeover = {"claimed": False}

                def stale_failure_handler(job):
                    current = db.session.get(ProcessingJob, job.id)
                    current.status = "failed"
                    current.worker_pid = None
                    current.error_message = "operator recovery"
                    db.session.commit()
                    retried = retry_failed_job(current)
                    second, claimed = _claim_pending_job(retried.id, worker_pid=4444)
                    failure_takeover["claimed"] = claimed and second.attempt_count == 2
                    raise RuntimeError("stale worker failure")

                after_stale_failure = execute_job(
                    stale_failure_id,
                    handlers={"analyze": stale_failure_handler},
                    worker_pid=3333,
                )
                failures += check(
                    "stale failing worker cannot fail newer attempt",
                    failure_takeover["claimed"]
                    and after_stale_failure.status == "running"
                    and after_stale_failure.attempt_count == 2
                    and after_stale_failure.worker_pid == 4444
                    and after_stale_failure.error_message is None,
                    str(after_stale_failure.to_dict()),
                )
                current, failed = _finish_job_failure(
                    stale_failure_id,
                    2,
                    RuntimeError("fresh worker failure"),
                )
                failures += check(
                    "current attempt can still persist its failure",
                    failed is True
                    and current.status == "failed"
                    and "fresh worker failure" in (current.error_message or ""),
                    str(current.to_dict()),
                )

                progress_job = ProcessingJob(
                    project_id=project_id,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(progress_job)
                db.session.commit()
                progress_id = progress_job.id
                first, claimed = _claim_pending_job(progress_id, worker_pid=5555)
                stale_snapshot = SimpleNamespace(id=progress_id, attempt_count=first.attempt_count)
                first.status = "failed"
                first.worker_pid = None
                first.error_message = "operator recovery"
                db.session.commit()
                retried = retry_failed_job(first)
                second, second_claimed = _claim_pending_job(retried.id, worker_pid=6666)

                stale_progress_rejected = False
                try:
                    update_progress(stale_snapshot, "stale_progress")
                except JobLeaseLost:
                    stale_progress_rejected = True
                current = db.session.get(ProcessingJob, progress_id)
                failures += check(
                    "stale worker progress is fenced",
                    claimed
                    and second_claimed
                    and stale_progress_rejected
                    and current.status == "running"
                    and current.attempt_count == 2
                    and (current.to_dict().get("progress") or {}).get("stage") != "stale_progress",
                    str(current.to_dict()),
                )

                update_progress(second, "fresh_progress", marker=7)
                current = db.session.get(ProcessingJob, progress_id)
                failures += check(
                    "current worker progress is accepted",
                    (current.to_dict().get("progress") or {}).get("stage") == "fresh_progress"
                    and (current.to_dict().get("progress") or {}).get("marker") == 7,
                    str(current.to_dict()),
                )
                _finish_job_success(progress_id, 2, {"ok": True})

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check("job fencing smoke", False, f"{type(exc).__name__}: {exc}")
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
