import hashlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def load_recovery_cli(repo_root: Path):
    path = repo_root / "scripts" / "recover_processing_job.py"
    spec = importlib.util.spec_from_file_location("recover_processing_job", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config

    original = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }
    failures = 0
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_job_recovery_") as tmp:
        root = Path(tmp)
        db_path = root / "recovery.db"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.job_conflicts import find_conflicting_active_job
            import services.job_recovery as recovery

            app = create_app()
            app.config["TESTING"] = True
            client = app.test_client()
            now = datetime.now(timezone.utc)

            failures += check(
                "current Python process is detected as alive",
                recovery.worker_pid_liveness(os.getpid()) is True,
            )

            live_pid = 11111
            dead_pid = 22222
            unknown_pid = 33333
            pid_states = {
                live_pid: True,
                dead_pid: False,
                unknown_pid: None,
            }

            def fake_pid_checker(pid):
                return pid_states.get(int(pid))

            with app.app_context():
                project = Project(name="Job recovery smoke")
                db.session.add(project)
                db.session.flush()
                project_id = project.id
                interview = Interview(project_id=project_id, status="pending")
                db.session.add(interview)
                db.session.flush()
                interview_id = interview.id

                fresh = ProcessingJob(
                    project_id=project_id,
                    job_type="project_pipeline",
                    status="pending",
                    created_at=now - timedelta(minutes=1),
                )
                stale_pending = ProcessingJob(
                    project_id=project_id,
                    job_type="transcribe",
                    status="pending",
                    created_at=now - timedelta(minutes=6),
                )
                stale_running_no_pid = ProcessingJob(
                    project_id=project_id,
                    job_type="analyze",
                    status="running",
                    created_at=now - timedelta(minutes=6),
                    started_at=now - timedelta(minutes=6),
                )
                live_running = ProcessingJob(
                    project_id=project_id,
                    job_type="map",
                    status="running",
                    worker_pid=live_pid,
                    created_at=now - timedelta(hours=24),
                    started_at=now - timedelta(hours=24),
                )
                dead_running = ProcessingJob(
                    project_id=project_id,
                    job_type="map",
                    status="running",
                    worker_pid=dead_pid,
                    created_at=now - timedelta(minutes=1),
                    started_at=now - timedelta(minutes=1),
                )
                unknown_running = ProcessingJob(
                    project_id=project_id,
                    job_type="analyze",
                    status="running",
                    worker_pid=unknown_pid,
                    created_at=now - timedelta(hours=24),
                    started_at=now - timedelta(hours=24),
                )
                db.session.add_all([
                    fresh,
                    stale_pending,
                    stale_running_no_pid,
                    live_running,
                    dead_running,
                    unknown_running,
                ])
                db.session.commit()
                fresh_id = fresh.id
                stale_pending_id = stale_pending.id
                stale_running_no_pid_id = stale_running_no_pid.id
                live_running_id = live_running.id
                dead_running_id = dead_running.id
                unknown_running_id = unknown_running.id

                failures += check(
                    "fresh pending job is not stale",
                    recovery.stale_reason(fresh, now=now, pid_checker=fake_pid_checker) is None,
                )
                failures += check(
                    "old pending job without worker is stale",
                    recovery.stale_reason(stale_pending, now=now, pid_checker=fake_pid_checker) is not None,
                )
                failures += check(
                    "old running job without worker is stale",
                    recovery.stale_reason(stale_running_no_pid, now=now, pid_checker=fake_pid_checker) is not None,
                )
                failures += check(
                    "confirmed live PID remains protected regardless of age",
                    recovery.stale_reason(live_running, now=now, pid_checker=fake_pid_checker) is None,
                )
                failures += check(
                    "confirmed dead PID is stale immediately",
                    recovery.stale_reason(dead_running, now=now, pid_checker=fake_pid_checker)
                    == "worker process is no longer running",
                )
                failures += check(
                    "inconclusive PID remains protected",
                    recovery.stale_reason(unknown_running, now=now, pid_checker=fake_pid_checker) is None,
                )

                recovered = recovery.recover_stale_jobs(
                    project_id=project_id,
                    pid_checker=fake_pid_checker,
                )
                recovered_ids = {job.id for job in recovered}
                failures += check(
                    "unambiguous stale and dead-worker jobs are recovered",
                    {stale_pending_id, stale_running_no_pid_id, dead_running_id}.issubset(recovered_ids)
                    and live_running_id not in recovered_ids
                    and unknown_running_id not in recovered_ids,
                    str(recovered_ids),
                )
                failures += check(
                    "fresh active job remains pending",
                    db.session.get(ProcessingJob, fresh_id).status == "pending",
                )
                failures += check(
                    "live PID-backed running job remains protected",
                    db.session.get(ProcessingJob, live_running_id).status == "running",
                )
                failures += check(
                    "inconclusive PID-backed running job remains protected",
                    db.session.get(ProcessingJob, unknown_running_id).status == "running",
                )
                failures += check(
                    "dead PID-backed job becomes retryable failed state",
                    db.session.get(ProcessingJob, dead_running_id).status == "failed"
                    and "worker process is no longer running"
                    in (db.session.get(ProcessingJob, dead_running_id).error_message or ""),
                )
                db.session.remove()

            before = file_hash(db_path)
            recovery_cli = load_recovery_cli(repo_root)
            inspected = recovery_cli._read_job(dead_running_id)
            after = file_hash(db_path)
            failures += check(
                "recovery CLI inspection uses read-only DB access",
                inspected is not None and before == after,
                f"before={before} after={after}",
            )

            retry_row = {
                "status": "pending",
                "worker_pid": None,
                "created_at": (now - timedelta(days=1)).isoformat(),
                "started_at": (now - timedelta(minutes=1)).isoformat(),
            }
            failures += check(
                "recovery CLI uses fresh retry started_at instead of historical created_at",
                recovery_cli._stale_reason_from_row(retry_row) is None,
            )
            retry_row["started_at"] = (now - timedelta(minutes=6)).isoformat()
            failures += check(
                "recovery CLI marks retry stale after current-attempt grace expires",
                recovery_cli._stale_reason_from_row(retry_row)
                == "active job has no worker after launch grace period",
            )

            original_checker = recovery.worker_pid_liveness
            recovery.worker_pid_liveness = fake_pid_checker
            try:
                with app.app_context():
                    for job_id in (fresh_id, live_running_id, unknown_running_id):
                        db.session.delete(db.session.get(ProcessingJob, job_id))

                    conflict_dead = ProcessingJob(
                        project_id=project_id,
                        interview_id=interview_id,
                        job_type="map",
                        status="running",
                        worker_pid=dead_pid,
                        created_at=now - timedelta(minutes=1),
                        started_at=now - timedelta(minutes=1),
                    )
                    db.session.add(conflict_dead)
                    db.session.commit()
                    conflict_dead_id = conflict_dead.id

                    conflict = find_conflicting_active_job(project_id, "analyze", interview_id)
                    failures += check(
                        "conflict check clears confirmed dead worker",
                        conflict is None
                        and db.session.get(ProcessingJob, conflict_dead_id).status == "failed",
                    )

                    status_dead = ProcessingJob(
                        project_id=project_id,
                        interview_id=interview_id,
                        job_type="transcribe",
                        status="running",
                        worker_pid=dead_pid,
                        created_at=now - timedelta(minutes=1),
                        started_at=now - timedelta(minutes=1),
                    )
                    db.session.add(status_dead)
                    db.session.commit()
                    status_dead_id = status_dead.id

                status_response = client.get(f"/api/interviews/{interview_id}/status")
                status_payload = status_response.get_json() or {}
                failures += check(
                    "interview status polling recovers confirmed dead worker",
                    status_response.status_code == 200
                    and status_payload.get("latest_job", {}).get("id") == status_dead_id
                    and status_payload.get("latest_job", {}).get("status") == "failed",
                    str(status_payload.get("latest_job")),
                )

                job_response = client.get(f"/api/processing-jobs/{status_dead_id}")
                job_payload = job_response.get_json() or {}
                failures += check(
                    "job polling returns dead-worker recovered failed state",
                    job_response.status_code == 200
                    and job_payload.get("job", {}).get("status") == "failed",
                    str(job_payload),
                )
            finally:
                recovery.worker_pid_liveness = original_checker
        except Exception as exc:
            failures += check("job recovery smoke", False, f"{type(exc).__name__}: {exc}")
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
