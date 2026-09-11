import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text


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

    with tempfile.TemporaryDirectory(prefix="qualia_recovery_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'recovery_fencing.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.job_recovery import _mark_job_failed_if_unchanged

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Recovery fencing smoke")
                db.session.add(project)
                db.session.commit()
                project_id = project.id

                def detached_snapshot(job: ProcessingJob) -> ProcessingJob:
                    # Force all CAS fields into memory, then detach so a later DB
                    # update cannot refresh this recovery observation implicitly.
                    _ = (job.id, job.status, job.attempt_count, job.worker_pid)
                    db.session.expunge(job)
                    db.session.rollback()
                    return job

                # Race 1: recovery observed running/dead, but the worker committed
                # success before recovery attempted its DB write.
                success_job = ProcessingJob(
                    project_id=project_id,
                    job_type="map",
                    status="running",
                    attempt_count=1,
                    worker_pid=4101,
                )
                db.session.add(success_job)
                db.session.commit()
                success_id = success_job.id
                stale_success = detached_snapshot(db.session.get(ProcessingJob, success_id))
                with db.engine.begin() as conn:
                    conn.execute(
                        text(
                            "UPDATE processing_jobs "
                            "SET status='succeeded', worker_pid=NULL, "
                            "result_json=:result_json, finished_at=:finished_at "
                            "WHERE id=:job_id"
                        ),
                        {
                            "job_id": success_id,
                            "result_json": '{"winner":"worker"}',
                            "finished_at": datetime.now(timezone.utc),
                        },
                    )
                current, changed = _mark_job_failed_if_unchanged(
                    stale_success,
                    "worker process is no longer running",
                )
                failures += check(
                    "worker success wins over stale recovery snapshot",
                    not changed
                    and current.status == "succeeded"
                    and current.worker_pid is None
                    and current.result_json == '{"winner":"worker"}',
                    str(current.to_dict()),
                )

                # Race 2: the old attempt was recovered/retried and a newer worker
                # already owns attempt 2. Recovery from attempt 1 must not fail it.
                retry_job = ProcessingJob(
                    project_id=project_id,
                    job_type="analyze",
                    status="running",
                    attempt_count=1,
                    worker_pid=4102,
                )
                db.session.add(retry_job)
                db.session.commit()
                retry_id = retry_job.id
                stale_retry = detached_snapshot(db.session.get(ProcessingJob, retry_id))
                with db.engine.begin() as conn:
                    conn.execute(
                        text(
                            "UPDATE processing_jobs "
                            "SET status='running', attempt_count=2, worker_pid=4202 "
                            "WHERE id=:job_id"
                        ),
                        {"job_id": retry_id},
                    )
                current, changed = _mark_job_failed_if_unchanged(
                    stale_retry,
                    "worker process is no longer running",
                )
                failures += check(
                    "newer attempt is fenced from old recovery decision",
                    not changed
                    and current.status == "running"
                    and current.attempt_count == 2
                    and current.worker_pid == 4202,
                    str(current.to_dict()),
                )

                # Race 3: an old pending/no-PID observation becomes invalid when a
                # launcher attaches the child PID before recovery writes.
                launch_job = ProcessingJob(
                    project_id=project_id,
                    job_type="project_pipeline",
                    status="pending",
                    attempt_count=0,
                    worker_pid=None,
                )
                db.session.add(launch_job)
                db.session.commit()
                launch_id = launch_job.id
                stale_launch = detached_snapshot(db.session.get(ProcessingJob, launch_id))
                with db.engine.begin() as conn:
                    conn.execute(
                        text(
                            "UPDATE processing_jobs "
                            "SET worker_pid=4303, progress_json=:progress "
                            "WHERE id=:job_id"
                        ),
                        {"job_id": launch_id, "progress": '{"stage":"worker_started"}'},
                    )
                current, changed = _mark_job_failed_if_unchanged(
                    stale_launch,
                    "active job has no worker after launch grace period",
                )
                failures += check(
                    "launcher PID attachment invalidates no-worker recovery decision",
                    not changed
                    and current.status == "pending"
                    and current.worker_pid == 4303,
                    str(current.to_dict()),
                )

                # Control: when the observed active state is still exactly the same,
                # recovery must still make the dead job retryable.
                dead_job = ProcessingJob(
                    project_id=project_id,
                    job_type="transcribe",
                    status="running",
                    attempt_count=3,
                    worker_pid=4404,
                )
                db.session.add(dead_job)
                db.session.commit()
                dead_id = dead_job.id
                stale_dead = detached_snapshot(db.session.get(ProcessingJob, dead_id))
                current, changed = _mark_job_failed_if_unchanged(
                    stale_dead,
                    "worker process is no longer running",
                )
                failures += check(
                    "unchanged dead worker is still recovered",
                    changed
                    and current.status == "failed"
                    and current.attempt_count == 3
                    and current.worker_pid is None
                    and "worker process is no longer running" in (current.error_message or ""),
                    str(current.to_dict()),
                )

                # Terminal rows are never valid recovery targets, even when the
                # helper is called directly with a refreshed object.
                terminal = db.session.get(ProcessingJob, success_id)
                current, changed = _mark_job_failed_if_unchanged(
                    terminal,
                    "should not apply",
                )
                failures += check(
                    "terminal state is never rewritten by recovery helper",
                    not changed and current.status == "succeeded",
                    str(current.to_dict()),
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "job recovery fencing smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
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
