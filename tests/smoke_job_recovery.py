import hashlib
import importlib.util
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
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.job_conflicts import find_conflicting_active_job
            from services.job_recovery import recover_stale_jobs, stale_reason

            app = create_app()
            now = datetime.now(timezone.utc)

            with app.app_context():
                project = Project(name="Job recovery smoke")
                db.session.add(project)
                db.session.flush()
                project_id = project.id

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
                stale_running = ProcessingJob(
                    project_id=project_id,
                    job_type="map",
                    status="running",
                    worker_pid=99999,
                    created_at=now - timedelta(hours=13),
                    started_at=now - timedelta(hours=13),
                )
                db.session.add_all([fresh, stale_pending, stale_running_no_pid, stale_running])
                db.session.commit()
                fresh_id = fresh.id
                stale_pending_id = stale_pending.id
                stale_running_no_pid_id = stale_running_no_pid.id
                stale_running_id = stale_running.id

                failures += check(
                    "fresh pending job is not stale",
                    stale_reason(fresh, now=now) is None,
                )
                failures += check(
                    "old pending job without worker is stale",
                    stale_reason(stale_pending, now=now) is not None,
                )
                failures += check(
                    "old running job without worker is stale",
                    stale_reason(stale_running_no_pid, now=now) is not None,
                )
                failures += check(
                    "very old running job is stale",
                    stale_reason(stale_running, now=now) is not None,
                )

                recovered = recover_stale_jobs(project_id=project_id)
                recovered_ids = {job.id for job in recovered}
                failures += check(
                    "stale jobs are recovered",
                    {stale_pending_id, stale_running_no_pid_id, stale_running_id}.issubset(recovered_ids),
                    str(recovered_ids),
                )
                failures += check(
                    "fresh active job remains pending",
                    db.session.get(ProcessingJob, fresh_id).status == "pending",
                )
                failures += check(
                    "recovered job becomes retryable failed state",
                    db.session.get(ProcessingJob, stale_pending_id).status == "failed"
                    and "job recovery:" in (db.session.get(ProcessingJob, stale_pending_id).error_message or ""),
                )

                db.session.remove()

            before = file_hash(db_path)
            recovery_cli = load_recovery_cli(repo_root)
            inspected = recovery_cli._read_job(stale_pending_id)
            after = file_hash(db_path)
            failures += check(
                "recovery CLI inspection uses read-only DB access",
                inspected is not None and before == after,
                f"before={before} after={after}",
            )

            with app.app_context():
                # Remove the intentionally fresh project-wide job, then verify a
                # stale conflicting row is cleared before conflict evaluation.
                db.session.delete(db.session.get(ProcessingJob, fresh_id))
                conflict_stale = ProcessingJob(
                    project_id=project_id,
                    interview_id=123,
                    job_type="map",
                    status="pending",
                    created_at=now - timedelta(minutes=6),
                )
                db.session.add(conflict_stale)
                db.session.commit()
                conflict_stale_id = conflict_stale.id

                conflict = find_conflicting_active_job(project_id, "analyze", 123)
                failures += check(
                    "conflict check clears stale blocker",
                    conflict is None
                    and db.session.get(ProcessingJob, conflict_stale_id).status == "failed",
                )
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
