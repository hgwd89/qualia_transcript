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
            from services.job_admission import admit_retry_job
            import routes.analysis_view as analysis_view
            import routes.analyze as analyze_route
            import routes.transcribe as transcribe_route
            import services.processing_jobs as processing_jobs
            import services.worker_launch_guard as launch_guard

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Worker launch fence")
                db.session.add(project)
                db.session.flush()

                def new_job(job_type: str = "project_pipeline") -> ProcessingJob:
                    job = ProcessingJob(
                        project_id=project.id,
                        job_type=job_type,
                        status="pending",
                        progress_json='{"stage":"queued"}',
                        attempt_count=0,
                    )
                    db.session.add(job)
                    db.session.commit()
                    return job

                original_launch = launch_guard.launch_job_worker
                try:
                    pre_spawn = new_job()

                    def fail_before_spawn(_job_id: int, **_kwargs):
                        raise RuntimeError("simulated spawn failure")

                    launch_guard.launch_job_worker = fail_before_spawn
                    pid, launch_error = launch_guard.launch_job_or_preserve_active(pre_spawn)
                    db.session.expire_all()
                    pre_after = db.session.get(ProcessingJob, int(pre_spawn.id))
                    failures += check(
                        "definite pre-spawn failure marks the untouched pending job failed",
                        launch_error is not None
                        and "simulated spawn failure" in str(launch_error)
                        and pid is None
                        and pre_after.status == "failed"
                        and pre_after.worker_pid is None
                        and "worker launch failed" in str(pre_after.error_message or ""),
                        (
                            f"error={launch_error!r} pid={pid!r} status={pre_after.status!r} "
                            f"worker_pid={pre_after.worker_pid!r}"
                        ),
                    )

                    reserved = new_job()
                    reserved_id = int(reserved.id)

                    def fail_after_launch_reservation(job_id: int, **_kwargs):
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

                    launch_guard.launch_job_worker = fail_after_launch_reservation
                    error, pid = transcribe_route._launch_or_fail(reserved)
                    db.session.expire_all()
                    reserved_after = db.session.get(ProcessingJob, reserved_id)
                    failures += check(
                        "transcription route preserves a launch-reserved pending job",
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

                    def child_claims_then_parent_bookkeeping_fails(job_id: int, **_kwargs):
                        current, did_claim = processing_jobs._claim_pending_job(
                            int(job_id),
                            worker_pid=43211,
                        )
                        assert did_claim
                        assert current.status == "running"
                        raise RuntimeError("simulated parent bookkeeping failure after child claim")

                    launch_guard.launch_job_worker = child_claims_then_parent_bookkeeping_fails
                    pid, launch_error = analysis_view._launch_or_fail(claimed_job)
                    db.session.expire_all()
                    claimed_after = db.session.get(ProcessingJob, claimed_id)
                    failures += check(
                        "project-analysis route cannot overwrite a live claimed worker lease",
                        launch_error is None
                        and pid == 43211
                        and claimed_after.status == "running"
                        and int(claimed_after.attempt_count or 0) == 1
                        and int(claimed_after.worker_pid or 0) == 43211
                        and claimed_after.finished_at is None,
                        (
                            f"error={launch_error!r} pid={pid!r} status={claimed_after.status!r} "
                            f"attempt={claimed_after.attempt_count!r} "
                            f"worker_pid={claimed_after.worker_pid!r}"
                        ),
                    )

                    individual_job = new_job("analyze")
                    individual_id = int(individual_job.id)
                    launch_guard.launch_job_worker = child_claims_then_parent_bookkeeping_fails
                    pid, launch_error = analyze_route._launch_or_fail(individual_job)
                    db.session.expire_all()
                    individual_after = db.session.get(ProcessingJob, individual_id)
                    failures += check(
                        "individual-analysis route cannot overwrite a live claimed worker lease",
                        launch_error is None
                        and pid == 43211
                        and individual_after.status == "running"
                        and int(individual_after.attempt_count or 0) == 1
                        and int(individual_after.worker_pid or 0) == 43211
                        and individual_after.finished_at is None,
                        (
                            f"error={launch_error!r} pid={pid!r} status={individual_after.status!r} "
                            f"attempt={individual_after.attempt_count!r} "
                            f"worker_pid={individual_after.worker_pid!r}"
                        ),
                    )

                    # A spawned parent/child pair can outlive the reservation it
                    # was created for. Reusing the same row for another pre-claim
                    # retry must not let the old parent record/release the new
                    # reservation or let the delayed old child claim it.
                    aba_project = Project(name="Worker launch generation ABA")
                    db.session.add(aba_project)
                    db.session.flush()
                    aba_job = ProcessingJob(
                        project_id=aba_project.id,
                        job_type="project_pipeline",
                        status="failed",
                        progress_json='{"stage":"failed"}',
                        error_message="old launch failed",
                        attempt_count=4,
                        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
                        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
                        finished_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    )
                    db.session.add(aba_job)
                    db.session.commit()
                    aba_id = int(aba_job.id)

                    first_retry = admit_retry_job(aba_id)
                    db.session.expire_all()
                    first_pending = db.session.get(ProcessingJob, aba_id)
                    first_attempt = int(first_pending.attempt_count or 0)
                    first_anchor = first_pending.started_at
                    _first_reserved, first_reserved = processing_jobs._reserve_worker_launch(
                        aba_id,
                        expected_attempt_count=first_attempt,
                        expected_started_at=first_anchor,
                    )

                    first_pending = db.session.get(ProcessingJob, aba_id)
                    first_pending.status = "failed"
                    first_pending.worker_pid = None
                    first_pending.progress_json = '{"stage":"failed"}'
                    first_pending.error_message = "reservation abandoned"
                    first_pending.finished_at = datetime.now(timezone.utc)
                    db.session.commit()

                    second_retry = admit_retry_job(aba_id)
                    db.session.expire_all()
                    second_pending = db.session.get(ProcessingJob, aba_id)
                    second_attempt = int(second_pending.attempt_count or 0)
                    second_anchor = second_pending.started_at
                    _second_reserved, second_reserved = processing_jobs._reserve_worker_launch(
                        aba_id,
                        expected_attempt_count=second_attempt,
                        expected_started_at=second_anchor,
                    )

                    stale_recorded = processing_jobs._record_worker_launch(
                        aba_id,
                        44001,
                        expected_attempt_count=first_attempt,
                        expected_started_at=first_anchor,
                    )
                    stale_released = processing_jobs._release_worker_launch_reservation(
                        aba_id,
                        expected_attempt_count=first_attempt,
                        expected_started_at=first_anchor,
                    )
                    _stale_child, stale_claimed = processing_jobs._claim_pending_job(
                        aba_id,
                        worker_pid=44001,
                        expected_attempt_count=first_attempt,
                        expected_started_at=first_anchor,
                    )
                    db.session.expire_all()
                    after_stale = db.session.get(ProcessingJob, aba_id)
                    after_stale_status = after_stale.status
                    after_stale_pid = after_stale.worker_pid
                    after_stale_anchor = after_stale.started_at
                    fresh_child, fresh_claimed = processing_jobs._claim_pending_job(
                        aba_id,
                        worker_pid=44002,
                        expected_attempt_count=second_attempt,
                        expected_started_at=second_anchor,
                    )
                    failures += check(
                        "stale parent and child cannot cross a reused launch reservation generation",
                        first_retry.created
                        and second_retry.created
                        and first_reserved
                        and second_reserved
                        and first_anchor is not None
                        and second_anchor is not None
                        and first_anchor != second_anchor
                        and not stale_recorded
                        and not stale_released
                        and not stale_claimed
                        and after_stale_status == "pending"
                        and int(after_stale_pid or 0) == 0
                        and after_stale_anchor == second_anchor
                        and fresh_claimed
                        and fresh_child.status == "running"
                        and int(fresh_child.worker_pid or 0) == 44002
                        and int(fresh_child.attempt_count or 0) == second_attempt + 1,
                        (
                            f"first={first_anchor!r} second={second_anchor!r} "
                            f"recorded={stale_recorded} released={stale_released} "
                            f"stale_claimed={stale_claimed} fresh_claimed={fresh_claimed} "
                            f"status={fresh_child.status!r} pid={fresh_child.worker_pid!r}"
                        ),
                    )

                    # Route-level pre-spawn error handling uses the same anchor.
                    # If the row cycles through failed and a new retry while the
                    # old launch call is unwinding, the old exception must not
                    # fail the fresh retry.
                    route_project = Project(name="Route launch ABA")
                    db.session.add(route_project)
                    db.session.flush()
                    route_job = ProcessingJob(
                        project_id=route_project.id,
                        job_type="project_pipeline",
                        status="failed",
                        progress_json='{"stage":"failed"}',
                        error_message="old route failure",
                        attempt_count=7,
                        created_at=datetime.now(timezone.utc) - timedelta(hours=3),
                        started_at=datetime.now(timezone.utc) - timedelta(hours=2),
                        finished_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    )
                    db.session.add(route_job)
                    db.session.commit()
                    route_id = int(route_job.id)
                    route_first = admit_retry_job(route_id)
                    db.session.expire_all()
                    route_first_pending = db.session.get(ProcessingJob, route_id)
                    route_first_anchor = route_first_pending.started_at
                    route_second_anchor = {"value": None}

                    def fail_after_row_reuse(job_id: int, **_kwargs):
                        current = db.session.get(ProcessingJob, int(job_id))
                        current.status = "failed"
                        current.worker_pid = None
                        current.progress_json = '{"stage":"failed"}'
                        current.error_message = "simulated old pre-spawn failure"
                        current.finished_at = datetime.now(timezone.utc)
                        db.session.commit()
                        admission = admit_retry_job(int(job_id))
                        assert admission.created
                        db.session.expire_all()
                        route_second_anchor["value"] = db.session.get(
                            ProcessingJob,
                            int(job_id),
                        ).started_at
                        raise RuntimeError("simulated old launcher exception")

                    launch_guard.launch_job_worker = fail_after_row_reuse
                    route_pid, route_error = launch_guard.launch_job_or_preserve_active(
                        route_first_pending,
                    )
                    db.session.expire_all()
                    route_after = db.session.get(ProcessingJob, route_id)
                    failures += check(
                        "route launch failure CAS cannot fail a later pre-claim retry",
                        route_first.created
                        and route_first_anchor is not None
                        and route_second_anchor["value"] is not None
                        and route_first_anchor != route_second_anchor["value"]
                        and route_pid is None
                        and route_error is None
                        and route_after.status == "pending"
                        and route_after.worker_pid is None
                        and route_after.started_at == route_second_anchor["value"],
                        (
                            f"first={route_first_anchor!r} second={route_second_anchor['value']!r} "
                            f"pid={route_pid!r} error={route_error!r} status={route_after.status!r}"
                        ),
                    )
                finally:
                    launch_guard.launch_job_worker = original_launch
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

    transcribe_source = (repo_root / "routes" / "transcribe.py").read_text(encoding="utf-8")
    project_analysis_source = (repo_root / "routes" / "analysis_view.py").read_text(encoding="utf-8")
    individual_analysis_source = (repo_root / "routes" / "analyze.py").read_text(encoding="utf-8")
    processing_source = (repo_root / "services" / "processing_jobs.py").read_text(encoding="utf-8")
    worker_script_source = (repo_root / "scripts" / "run_processing_job.py").read_text(encoding="utf-8")
    failures += check(
        "all HTTP worker launch routes use the shared durable launch guard",
        "from services.worker_launch_guard import launch_job_or_preserve_active" in transcribe_source
        and "from services.worker_launch_guard import launch_job_or_preserve_active" in project_analysis_source
        and "from services.worker_launch_guard import launch_job_or_preserve_active" in individual_analysis_source
        and "from services.processing_jobs import launch_job_worker" not in transcribe_source
        and "from services.processing_jobs import launch_job_worker" not in project_analysis_source
        and "from services.processing_jobs import launch_job_worker" not in individual_analysis_source,
    )

    failures += check(
        "detached worker command and claim carry the launch generation token",
        '"--expected-attempt-count"' in processing_source
        and '"--expected-started-at"' in processing_source
        and 'parser.add_argument("--expected-attempt-count", type=int, required=True)' in worker_script_source
        and 'parser.add_argument("--expected-started-at", required=True)' in worker_script_source
        and "expected_attempt_count=args.expected_attempt_count" in worker_script_source
        and "expected_started_at=expected_started_at" in worker_script_source,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
