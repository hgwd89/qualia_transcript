from __future__ import annotations

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
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_worker_launch_fence_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'worker-launch.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        config.BACKUP_DIR = str(root / "backups")

        try:
            from app import create_app
            from models import db
            from models.processing_job import ProcessingJob
            from models.project import Project
            import routes.transcribe as transcribe_route
            import services.processing_jobs as processing_jobs

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Worker launch fence")
                db.session.add(project)
                db.session.flush()

                def new_job() -> ProcessingJob:
                    job = ProcessingJob(
                        project_id=project.id,
                        job_type="project_pipeline",
                        status="pending",
                        progress_json='{"stage":"queued"}',
                        attempt_count=0,
                    )
                    db.session.add(job)
                    db.session.commit()
                    return job

                original_launch = transcribe_route.launch_job_worker
                try:
                    pre_spawn = new_job()

                    def fail_before_spawn(_job_id: int):
                        raise RuntimeError("simulated spawn failure")

                    transcribe_route.launch_job_worker = fail_before_spawn
                    error, pid = transcribe_route._launch_or_fail(pre_spawn)
                    db.session.expire_all()
                    pre_after = db.session.get(ProcessingJob, int(pre_spawn.id))
                    failures += check(
                        "definite pre-spawn failure marks the untouched pending job failed",
                        bool(error)
                        and "simulated spawn failure" in str(error)
                        and pid is None
                        and pre_after.status == "failed"
                        and pre_after.worker_pid is None
                        and "worker launch failed" in str(pre_after.error_message or ""),
                        (
                            f"error={error!r} pid={pid!r} status={pre_after.status!r} "
                            f"worker_pid={pre_after.worker_pid!r}"
                        ),
                    )

                    reserved = new_job()
                    reserved_id = int(reserved.id)

                    def fail_after_launch_reservation(job_id: int):
                        changed = (
                            ProcessingJob.query
                            .filter_by(id=int(job_id), status="pending")
                            .update(
                                {
                                    ProcessingJob.worker_pid: 0,
                                    ProcessingJob.progress_json: '{"stage":"launching"}',
                                },
                                synchronize_session=False,
                            )
                        )
                        db.session.commit()
                        assert changed == 1
                        raise RuntimeError("simulated post-spawn bookkeeping failure")

                    transcribe_route.launch_job_worker = fail_after_launch_reservation
                    error, pid = transcribe_route._launch_or_fail(reserved)
                    db.session.expire_all()
                    reserved_after = db.session.get(ProcessingJob, reserved_id)
                    failures += check(
                        "launch-reserved pending job is never overwritten failed by parent bookkeeping error",
                        error is None
                        and pid is None
                        and reserved_after.status == "pending"
                        and int(reserved_after.worker_pid or 0) == 0,
                        (
                            f"error={error!r} pid={pid!r} status={reserved_after.status!r} "
                            f"worker_pid={reserved_after.worker_pid!r}"
                        ),
                    )

                    claimed_after_reservation, claimed = processing_jobs._claim_pending_job(
                        reserved_id,
                        worker_pid=43210,
                    )
                    failures += check(
                        "child can still claim a launch-reserved job after parent bookkeeping failure",
                        claimed
                        and claimed_after_reservation.status == "running"
                        and int(claimed_after_reservation.attempt_count or 0) == 1
                        and int(claimed_after_reservation.worker_pid or 0) == 43210,
                        (
                            f"claimed={claimed} status={claimed_after_reservation.status!r} "
                            f"attempt={claimed_after_reservation.attempt_count!r} "
                            f"worker_pid={claimed_after_reservation.worker_pid!r}"
                        ),
                    )

                    claimed_job = new_job()
                    claimed_id = int(claimed_job.id)

                    def child_claims_then_parent_bookkeeping_fails(job_id: int):
                        current, did_claim = processing_jobs._claim_pending_job(
                            int(job_id),
                            worker_pid=43211,
                        )
                        assert did_claim
                        assert current.status == "running"
                        raise RuntimeError("simulated parent bookkeeping failure after child claim")

                    transcribe_route.launch_job_worker = child_claims_then_parent_bookkeeping_fails
                    error, pid = transcribe_route._launch_or_fail(claimed_job)
                    db.session.expire_all()
                    claimed_after = db.session.get(ProcessingJob, claimed_id)
                    failures += check(
                        "parent launcher error cannot overwrite a live claimed worker lease",
                        error is None
                        and pid == 43211
                        and claimed_after.status == "running"
                        and int(claimed_after.attempt_count or 0) == 1
                        and int(claimed_after.worker_pid or 0) == 43211
                        and claimed_after.finished_at is None,
                        (
                            f"error={error!r} pid={pid!r} status={claimed_after.status!r} "
                            f"attempt={claimed_after.attempt_count!r} "
                            f"worker_pid={claimed_after.worker_pid!r}"
                        ),
                    )
                finally:
                    transcribe_route.launch_job_worker = original_launch
                    db.session.remove()
                    db.engine.dispose()

        except Exception as exc:
            failures += check(
                "worker launch fencing smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
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
            config.BACKUP_DIR = original["BACKUP_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
