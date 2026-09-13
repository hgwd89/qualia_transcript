from __future__ import annotations

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


# services/processing_jobs.py: carry the retry-generation identity from route
# admission through reservation, spawn bookkeeping, and child claim.
replace_once(
    "services/processing_jobs.py",
    '''def _reserve_worker_launch(job_id: int) -> tuple[ProcessingJob, bool]:
    reserved = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .filter(ProcessingJob.worker_pid.is_(None))
        .update(
            {
                ProcessingJob.worker_pid: _LAUNCH_RESERVED_PID,
                ProcessingJob.progress_json: _json_dump({"stage": "launching"}),
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    return _refresh_job(job_id), reserved == 1


def _release_worker_launch_reservation(job_id: int) -> None:
    (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status.in_(ACTIVE_STATUSES))
        .filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
        .update(
            {
                ProcessingJob.worker_pid: None,
                ProcessingJob.progress_json: _json_dump({"stage": "queued"}),
            },
            synchronize_session=False,
        )
    )
    db.session.commit()
    db.session.expire_all()


def _record_worker_launch(job_id: int, pid: int) -> bool:
    pid = int(pid)
    pending_updated = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
        .update(
            {
                ProcessingJob.worker_pid: pid,
                ProcessingJob.progress_json: _json_dump({"stage": "worker_started", "pid": pid}),
            },
            synchronize_session=False,
        )
    )
    running_updated = 0
    if not pending_updated:
        running_updated = (
            ProcessingJob.query
            .filter(ProcessingJob.id == job_id)
            .filter(ProcessingJob.status == "running")
            .filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
            .update(
                {ProcessingJob.worker_pid: pid},
                synchronize_session=False,
            )
        )
    db.session.commit()
    db.session.expire_all()
    return bool(pending_updated or running_updated)


def launch_job_worker(job_id: int) -> int:
''',
    '''def _filter_launch_generation(query, attempt_count: int, started_at):
    query = query.filter(ProcessingJob.attempt_count == int(attempt_count))
    if started_at is None:
        return query.filter(ProcessingJob.started_at.is_(None))
    return query.filter(ProcessingJob.started_at == started_at)


def _reserve_worker_launch(
    job_id: int,
    *,
    expected_attempt_count: int | None = None,
    expected_started_at=None,
) -> tuple[ProcessingJob, bool]:
    query = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .filter(ProcessingJob.worker_pid.is_(None))
    )
    if expected_attempt_count is not None:
        query = _filter_launch_generation(query, expected_attempt_count, expected_started_at)
    reserved = query.update(
        {
            ProcessingJob.worker_pid: _LAUNCH_RESERVED_PID,
            ProcessingJob.progress_json: _json_dump({"stage": "launching"}),
        },
        synchronize_session=False,
    )
    db.session.commit()
    return _refresh_job(job_id), reserved == 1


def _release_worker_launch_reservation(
    job_id: int,
    *,
    expected_attempt_count: int,
    expected_started_at,
) -> bool:
    query = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
    )
    query = _filter_launch_generation(query, expected_attempt_count, expected_started_at)
    updated = query.update(
        {
            ProcessingJob.worker_pid: None,
            ProcessingJob.progress_json: _json_dump({"stage": "queued"}),
        },
        synchronize_session=False,
    )
    db.session.commit()
    db.session.expire_all()
    return updated == 1


def _record_worker_launch(
    job_id: int,
    pid: int,
    *,
    expected_attempt_count: int,
    expected_started_at,
) -> bool:
    # Parent bookkeeping belongs only to the exact pending reservation it
    # created. A claimed production child writes its own real PID atomically, so
    # there is no safe need for a parent-side running-state fallback.
    pid = int(pid)
    query = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
    )
    query = _filter_launch_generation(query, expected_attempt_count, expected_started_at)
    updated = query.update(
        {
            ProcessingJob.worker_pid: pid,
            ProcessingJob.progress_json: _json_dump({"stage": "worker_started", "pid": pid}),
        },
        synchronize_session=False,
    )
    db.session.commit()
    db.session.expire_all()
    return updated == 1


def launch_job_worker(
    job_id: int,
    *,
    expected_attempt_count: int | None = None,
    expected_started_at=None,
) -> int:
''',
)

replace_once(
    "services/processing_jobs.py",
    '''    job, reserved = _reserve_worker_launch(job_id)
    if not reserved:
        if job.status in ACTIVE_STATUSES:
            return int(job.worker_pid or 0)
        raise ValueError(f"job cannot be launched from status={job.status}")

    logs_dir = root / "logs"
''',
    '''    job, reserved = _reserve_worker_launch(
        job_id,
        expected_attempt_count=expected_attempt_count,
        expected_started_at=expected_started_at,
    )
    if not reserved:
        if job.status in ACTIVE_STATUSES:
            return int(job.worker_pid or 0)
        raise ValueError(f"job cannot be launched from status={job.status}")

    reservation_attempt_count = int(job.attempt_count or 0)
    reservation_started_at = job.started_at

    logs_dir = root / "logs"
''',
)

replace_once(
    "services/processing_jobs.py",
    '''            process = subprocess.Popen(
                [sys.executable, str(script), "--job-id", str(job.id)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
    except Exception:
        _release_worker_launch_reservation(job_id)
        raise

    _record_worker_launch(job_id, process.pid)
''',
    '''            process = subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--job-id",
                    str(job.id),
                    "--expected-attempt-count",
                    str(reservation_attempt_count),
                    "--expected-started-at",
                    reservation_started_at.isoformat() if reservation_started_at is not None else "none",
                ],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
    except Exception:
        _release_worker_launch_reservation(
            job_id,
            expected_attempt_count=reservation_attempt_count,
            expected_started_at=reservation_started_at,
        )
        raise

    _record_worker_launch(
        job_id,
        process.pid,
        expected_attempt_count=reservation_attempt_count,
        expected_started_at=reservation_started_at,
    )
''',
)

replace_once(
    "services/processing_jobs.py",
    '''def _claim_pending_job(job_id: int, worker_pid: int | None = None) -> tuple[ProcessingJob, bool]:
    values = {
        ProcessingJob.status: "running",
        ProcessingJob.started_at: _utcnow(),
        ProcessingJob.finished_at: None,
        ProcessingJob.attempt_count: ProcessingJob.attempt_count + 1,
        ProcessingJob.error_message: None,
    }
    if worker_pid is not None and int(worker_pid) > 0:
        values[ProcessingJob.worker_pid] = int(worker_pid)

    claimed = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
        .update(values, synchronize_session=False)
    )
''',
    '''def _claim_pending_job(
    job_id: int,
    worker_pid: int | None = None,
    *,
    expected_attempt_count: int | None = None,
    expected_started_at=None,
) -> tuple[ProcessingJob, bool]:
    values = {
        ProcessingJob.status: "running",
        ProcessingJob.started_at: _utcnow(),
        ProcessingJob.finished_at: None,
        ProcessingJob.attempt_count: ProcessingJob.attempt_count + 1,
        ProcessingJob.error_message: None,
    }
    if worker_pid is not None and int(worker_pid) > 0:
        values[ProcessingJob.worker_pid] = int(worker_pid)

    query = (
        ProcessingJob.query
        .filter(ProcessingJob.id == job_id)
        .filter(ProcessingJob.status == "pending")
    )
    if expected_attempt_count is not None:
        # Detached production children may claim only the exact launch
        # reservation that spawned them. This prevents a delayed old child from
        # claiming a later retry of the same durable job row.
        query = query.filter(ProcessingJob.worker_pid == _LAUNCH_RESERVED_PID)
        query = _filter_launch_generation(query, expected_attempt_count, expected_started_at)
    claimed = query.update(values, synchronize_session=False)
''',
)

replace_once(
    "services/processing_jobs.py",
    '''def execute_job(
    job_id: int,
    handlers: dict[str, object] | None = None,
    *,
    worker_pid: int | None = None,
) -> ProcessingJob:
    job, claimed = _claim_pending_job(job_id, worker_pid=worker_pid)
''',
    '''def execute_job(
    job_id: int,
    handlers: dict[str, object] | None = None,
    *,
    worker_pid: int | None = None,
    expected_attempt_count: int | None = None,
    expected_started_at=None,
) -> ProcessingJob:
    job, claimed = _claim_pending_job(
        job_id,
        worker_pid=worker_pid,
        expected_attempt_count=expected_attempt_count,
        expected_started_at=expected_started_at,
    )
''',
)

# The HTTP boundary must also identify the exact retry it admitted before it is
# allowed either to reserve launch or to declare a definite pre-spawn failure.
replace_once(
    "services/worker_launch_guard.py",
    '''    job_id = int(job.id)
    observed_attempt = int(job.attempt_count or 0)

    try:
        return launch_job_worker(job_id), None
    except Exception as exc:
''',
    '''    job_id = int(job.id)
    observed_attempt = int(job.attempt_count or 0)
    observed_started_at = job.started_at

    try:
        return launch_job_worker(
            job_id,
            expected_attempt_count=observed_attempt,
            expected_started_at=observed_started_at,
        ), None
    except Exception as exc:
''',
)

replace_once(
    "services/worker_launch_guard.py",
    '''        updated = (
            ProcessingJob.query
            .filter(ProcessingJob.id == job_id)
            .filter(ProcessingJob.status == "pending")
            .filter(ProcessingJob.attempt_count == observed_attempt)
            .filter(ProcessingJob.worker_pid.is_(None))
            .update(
''',
    '''        query = (
            ProcessingJob.query
            .filter(ProcessingJob.id == job_id)
            .filter(ProcessingJob.status == "pending")
            .filter(ProcessingJob.attempt_count == observed_attempt)
            .filter(ProcessingJob.worker_pid.is_(None))
        )
        if observed_started_at is None:
            query = query.filter(ProcessingJob.started_at.is_(None))
        else:
            query = query.filter(ProcessingJob.started_at == observed_started_at)
        updated = query.update(
''',
)

# Detached children must receive the reservation generation they are authorized
# to claim, rather than only the reusable database row id.
replace_once(
    "scripts/run_processing_job.py",
    '''import argparse
import os
import sys
from pathlib import Path
''',
    '''import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
''',
)
replace_once(
    "scripts/run_processing_job.py",
    '''    parser = argparse.ArgumentParser(description="Execute one durable Qualia processing job")
    parser.add_argument("--job-id", type=int, required=True)
    args = parser.parse_args()

    # Detached workers can outlive the Flask process. Keep a shared runtime lock
''',
    '''    parser = argparse.ArgumentParser(description="Execute one durable Qualia processing job")
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--expected-attempt-count", type=int, required=True)
    parser.add_argument("--expected-started-at", required=True)
    args = parser.parse_args()
    expected_started_at = (
        None
        if args.expected_started_at == "none"
        else datetime.fromisoformat(args.expected_started_at)
    )

    # Detached workers can outlive the Flask process. Keep a shared runtime lock
''',
)
replace_once(
    "scripts/run_processing_job.py",
    '''            job = execute_job(args.job_id, worker_pid=os.getpid())
''',
    '''            job = execute_job(
                args.job_id,
                worker_pid=os.getpid(),
                expected_attempt_count=args.expected_attempt_count,
                expected_started_at=expected_started_at,
            )
''',
)

# Update existing launch stubs for the new exact-generation call signature.
for old, new in [
    ('def fail_before_spawn(_job_id: int):', 'def fail_before_spawn(_job_id: int, **_kwargs):'),
    ('def fail_after_launch_reservation(job_id: int):', 'def fail_after_launch_reservation(job_id: int, **_kwargs):'),
    ('def child_claims_then_parent_bookkeeping_fails(job_id: int):', 'def child_claims_then_parent_bookkeeping_fails(job_id: int, **_kwargs):'),
]:
    replace_once("tests/smoke_worker_launch_fencing.py", old, new)

replace_once(
    "tests/smoke_worker_launch_fencing.py",
    '''import sys
import tempfile
from pathlib import Path
''',
    '''import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
''',
)
replace_once(
    "tests/smoke_worker_launch_fencing.py",
    '''            from models.processing_job import ProcessingJob
            from models.project import Project
            import routes.analysis_view as analysis_view
''',
    '''            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.job_admission import admit_retry_job
            import routes.analysis_view as analysis_view
''',
)

# Add an end-to-end ABA reproduction before the launch stub is restored.
replace_once(
    "tests/smoke_worker_launch_fencing.py",
    '''                    failures += check(
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
                finally:
''',
    '''                    failures += check(
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
                        and after_stale.status == "pending"
                        and int(after_stale.worker_pid or 0) == 0
                        and after_stale.started_at == second_anchor
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
''',
)

replace_once(
    "tests/smoke_worker_launch_fencing.py",
    '''    individual_analysis_source = (repo_root / "routes" / "analyze.py").read_text(encoding="utf-8")
    failures += check(
''',
    '''    individual_analysis_source = (repo_root / "routes" / "analyze.py").read_text(encoding="utf-8")
    processing_source = (repo_root / "services" / "processing_jobs.py").read_text(encoding="utf-8")
    worker_script_source = (repo_root / "scripts" / "run_processing_job.py").read_text(encoding="utf-8")
    failures += check(
''',
)
replace_once(
    "tests/smoke_worker_launch_fencing.py",
    '''    if failures:
        print(f"\\nSummary: FAIL ({failures} checks failed)")
''',
    '''    failures += check(
        "detached worker command and claim carry the launch generation token",
        '"--expected-attempt-count"' in processing_source
        and '"--expected-started-at"' in processing_source
        and 'parser.add_argument("--expected-attempt-count", type=int, required=True)' in worker_script_source
        and 'parser.add_argument("--expected-started-at", required=True)' in worker_script_source
        and "expected_attempt_count=args.expected_attempt_count" in worker_script_source
        and "expected_started_at=expected_started_at" in worker_script_source,
    )

    if failures:
        print(f"\\nSummary: FAIL ({failures} checks failed)")
''',
)

# Documentation: define the reservation generation as part of the launch contract.
replace_once(
    "docs/worker-launch-fencing.md",
    '''A launcher exception may mark a job `failed` only when the exact admitted row is still:

- `status='pending'`;
- on the same observed `attempt_count`; and
- `worker_pid IS NULL`.

That compare-and-swap identifies a definite pre-spawn/unclaimed failure.
''',
    '''A launcher exception may mark a job `failed` only when the exact admitted row is still:

- `status='pending'`;
- on the same observed `attempt_count`;
- on the same observed `started_at` current-retry anchor; and
- `worker_pid IS NULL`.

That compare-and-swap identifies a definite pre-spawn/unclaimed failure without allowing an old route invocation to fail a later retry of the same row.
''',
)
replace_once(
    "docs/worker-launch-fencing.md",
    '''The child remains authoritative for `pending -> running`: `_claim_pending_job()` increments `attempt_count` and attaches its real PID atomically. If the parent fails after reserving launch but before recording the PID, the child can still replace the sentinel and claim the job. If no child ever claims, existing stale-job recovery handles the abandoned active row after its grace/liveness checks.
''',
    '''The child remains authoritative for `pending -> running`: `_claim_pending_job()` increments `attempt_count` and attaches its real PID atomically. The parent passes the reserved row's `attempt_count` plus `started_at` anchor to the detached child, and the child must match that exact `worker_pid=0` reservation before claim. Parent PID recording and reservation release use the same generation token. A delayed parent or child from an abandoned launch therefore cannot mutate or claim a later retry that reused the same durable row. If the parent fails after reserving launch but before recording the PID, the correctly generated child can still replace the sentinel and claim the job. If no child ever claims, existing stale-job recovery handles the abandoned active row after its grace/liveness checks.
''',
)
replace_once(
    "docs/worker-launch-fencing.md",
    '''- a child that already claimed `running` cannot be overwritten by a later parent launcher exception; and
- transcription/mapping/project-pipeline, per-interview/per-question analysis, and project-level cross/integrated analysis routes all use the shared launch guard.
''',
    '''- a child that already claimed `running` cannot be overwritten by a later parent launcher exception;
- a failed→retry ABA before child claim cannot be crossed by stale parent PID bookkeeping, stale reservation release, a delayed old child, or an old route-level failure CAS; and
- transcription/mapping/project-pipeline, per-interview/per-question analysis, and project-level cross/integrated analysis routes all use the shared launch guard.
''',
)

print("worker launch generation patch applied")
