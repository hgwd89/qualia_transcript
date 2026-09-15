from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
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
    from scripts import recover_processing_job

    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
        "RUNTIME_LOCK_PATH": config.RUNTIME_LOCK_PATH,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_operator_recovery_fence_") as tmp:
        root = Path(tmp)
        db_path = root / "recovery.db"
        config.DATABASE_URI = f"sqlite:///{db_path.as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        config.BACKUP_DIR = str(root / "backups")
        config.RUNTIME_LOCK_PATH = str(root / "runtime.lock")

        try:
            from app import create_app
            from models import db
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.job_recovery import mark_job_failed_if_unchanged
            from services.runtime_lock import release_process_runtime_locks

            app = create_app()
            with app.app_context():
                project = Project(name="operator recovery fence", client="test")
                db.session.add(project)
                db.session.flush()

                t0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
                stale_job = ProcessingJob(
                    project_id=project.id,
                    job_type="map",
                    status="pending",
                    attempt_count=0,
                    worker_pid=None,
                    started_at=t0,
                )
                exact_job = ProcessingJob(
                    project_id=project.id,
                    job_type="analyze",
                    status="pending",
                    attempt_count=1,
                    worker_pid=None,
                    started_at=t0 + timedelta(minutes=1),
                )
                identity_job = ProcessingJob(
                    project_id=project.id,
                    job_type="analyze_semantic",
                    status="pending",
                    attempt_count=2,
                    worker_pid=None,
                    started_at=t0 + timedelta(minutes=2),
                )
                db.session.add_all([stale_job, exact_job, identity_job])
                db.session.commit()

                stale_id = int(stale_job.id)
                exact_id = int(exact_job.id)
                identity_id = int(identity_job.id)

                stale_row = recover_processing_job._read_job(stale_id)
                exact_row = recover_processing_job._read_job(exact_id)
                failures += check("stale fixture is inspectable", stale_row is not None)
                failures += check("exact fixture is inspectable", exact_row is not None)
                stale_token = recover_processing_job._recovery_generation_token_from_row(stale_row or {})
                exact_token = recover_processing_job._recovery_generation_token_from_row(exact_row or {})

                # Simulate retry admission / worker-generation movement after the
                # operator saw the row but before --apply reaches the write path.
                stale_job.started_at = t0 + timedelta(minutes=10)
                db.session.commit()

                # Exercise the lower-level CAS against row-identity replacement:
                # all active-attempt fields match, but created_at no longer names
                # the durable row generation that was inspected.
                original_created_at = identity_job.created_at
                observed = ProcessingJob(
                    id=identity_id,
                    project_id=project.id,
                    job_type=identity_job.job_type,
                    status=identity_job.status,
                    attempt_count=identity_job.attempt_count,
                    worker_pid=identity_job.worker_pid,
                    created_at=original_created_at,
                    started_at=identity_job.started_at,
                )
                identity_job.created_at = original_created_at + timedelta(seconds=1)
                db.session.commit()
                current_identity, identity_changed = mark_job_failed_if_unchanged(
                    observed,
                    "stale observed row identity",
                )
                failures += check(
                    "service CAS rejects changed durable row identity",
                    not identity_changed and current_identity.status == "pending",
                    f"changed={identity_changed}, status={current_identity.status}",
                )

                db.session.remove()
                db.engine.dispose()
            release_process_runtime_locks()

            stale_exit = recover_processing_job._apply_recovery(stale_id, stale_token)
            release_process_runtime_locks()
            failures += check(
                "operator recovery rejects generation changed after inspection",
                stale_exit == 4,
                f"exit={stale_exit}",
            )
            stale_after = recover_processing_job._read_job(stale_id)
            failures += check(
                "newer active generation remains untouched",
                stale_after is not None
                and stale_after.get("status") == "pending"
                and recover_processing_job._dt_token(stale_after.get("started_at"))
                == recover_processing_job._dt_token(t0 + timedelta(minutes=10)),
                f"row={stale_after!r}",
            )

            exact_exit = recover_processing_job._apply_recovery(exact_id, exact_token)
            release_process_runtime_locks()
            failures += check(
                "operator recovery can fail the exact inspected generation",
                exact_exit == 0,
                f"exit={exact_exit}",
            )
            exact_after = recover_processing_job._read_job(exact_id)
            failures += check(
                "exact inspected generation is marked failed",
                exact_after is not None
                and exact_after.get("status") == "failed"
                and "operator explicitly recovered active job"
                in str(exact_after.get("error_message") or ""),
                f"row={exact_after!r}",
            )

            recovery_source = (repo_root / "scripts" / "recover_processing_job.py").read_text(
                encoding="utf-8"
            )
            service_source = (repo_root / "services" / "job_recovery.py").read_text(
                encoding="utf-8"
            )
            failures += check(
                "operator inspection uses SQLite query-only mode",
                'PRAGMA query_only=ON' in recovery_source,
            )
            failures += check(
                "operator apply carries the inspected generation into write path",
                "inspected_generation = _recovery_generation_token_from_row(row)" in recovery_source
                and "_apply_recovery(args.job_id, inspected_generation)" in recovery_source,
            )
            failures += check(
                "recovery CAS pins durable row identity and active generation",
                "ProcessingJob.created_at == job.created_at" in service_source
                and "ProcessingJob.started_at == job.started_at" in service_source
                and "ProcessingJob.attempt_count == int(job.attempt_count or 0)" in service_source,
            )
        finally:
            try:
                from services.runtime_lock import release_process_runtime_locks

                release_process_runtime_locks()
            except Exception:
                pass
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]
            config.BACKUP_DIR = original_config["BACKUP_DIR"]
            config.RUNTIME_LOCK_PATH = original_config["RUNTIME_LOCK_PATH"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
