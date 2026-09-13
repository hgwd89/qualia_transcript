from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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

    with tempfile.TemporaryDirectory(prefix="qualia_retry_recovery_grace_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'retry-grace.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        config.BACKUP_DIR = str(root / "backups")

        try:
            from app import create_app
            from models import db
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.job_admission import admit_retry_job
            from services.job_recovery import (
                ACTIVE_WITHOUT_WORKER_GRACE,
                _mark_job_failed_if_unchanged,
                recover_stale_jobs,
                stale_reason,
            )
            from services.processing_jobs import _claim_pending_job

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Retry recovery grace")
                db.session.add(project)
                db.session.flush()

                old_created = datetime.now(timezone.utc) - timedelta(hours=2)
                old_failed = datetime.now(timezone.utc) - timedelta(hours=1)
                job = ProcessingJob(
                    project_id=project.id,
                    job_type="project_pipeline",
                    status="failed",
                    progress_json='{"stage":"failed"}',
                    error_message="old failure",
                    attempt_count=2,
                    created_at=old_created,
                    started_at=old_created + timedelta(minutes=1),
                    finished_at=old_failed,
                )
                db.session.add(job)
                db.session.commit()
                job_id = int(job.id)
                project_id = int(project.id)

                admission = admit_retry_job(job_id)
                db.session.expire_all()
                retried = db.session.get(ProcessingJob, job_id)
                retry_anchor = _utc(retried.started_at)
                created_anchor = _utc(retried.created_at)

                failures += check(
                    "retry admission records a fresh current-attempt grace anchor without rewriting creation time",
                    admission.created
                    and not admission.error
                    and retried.status == "pending"
                    and retry_anchor is not None
                    and created_anchor is not None
                    and retry_anchor - created_anchor > timedelta(hours=1),
                    (
                        f"created={created_anchor!r} started={retry_anchor!r} "
                        f"status={retried.status!r} admission={admission}"
                    ),
                )

                early_reason = stale_reason(
                    retried,
                    now=retry_anchor + timedelta(minutes=1),
                )
                failures += check(
                    "old retried job is not immediately stale during its fresh no-worker grace",
                    early_reason is None,
                    f"reason={early_reason!r}",
                )

                recovered = recover_stale_jobs(project_id=project_id)
                db.session.expire_all()
                retried = db.session.get(ProcessingJob, job_id)
                failures += check(
                    "normal recovery pass does not fail a just-retried old job",
                    not recovered and retried.status == "pending",
                    f"recovered={[item.id for item in recovered]} status={retried.status!r}",
                )

                # Reproduce an ABA window while no worker has claimed the durable
                # row yet. attempt_count does not increment until worker claim, so
                # two retry admissions can otherwise look identical to a stale
                # recovery observer unless started_at participates in the CAS.
                aba_project = Project(name="Retry recovery ABA")
                db.session.add(aba_project)
                db.session.flush()
                aba_job = ProcessingJob(
                    project_id=aba_project.id,
                    job_type="project_pipeline",
                    status="failed",
                    progress_json='{"stage":"failed"}',
                    error_message="first failure",
                    attempt_count=4,
                    created_at=datetime.now(timezone.utc) - timedelta(hours=3),
                    started_at=datetime.now(timezone.utc) - timedelta(hours=2),
                    finished_at=datetime.now(timezone.utc) - timedelta(hours=1),
                )
                db.session.add(aba_job)
                db.session.commit()
                aba_job_id = int(aba_job.id)

                first_aba_admission = admit_retry_job(aba_job_id)
                db.session.expire_all()
                first_pending = db.session.get(ProcessingJob, aba_job_id)
                first_anchor = first_pending.started_at
                observed = SimpleNamespace(
                    id=aba_job_id,
                    status=first_pending.status,
                    attempt_count=int(first_pending.attempt_count or 0),
                    worker_pid=first_pending.worker_pid,
                    started_at=first_anchor,
                )

                first_pending.status = "failed"
                first_pending.progress_json = '{"stage":"failed"}'
                first_pending.error_message = "second pre-claim failure"
                first_pending.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                second_aba_admission = admit_retry_job(aba_job_id)
                db.session.expire_all()
                second_pending = db.session.get(ProcessingJob, aba_job_id)
                second_anchor = second_pending.started_at

                _current, stale_changed = _mark_job_failed_if_unchanged(
                    observed,
                    "stale recovery observation from previous retry",
                )
                db.session.expire_all()
                after_stale_cas = db.session.get(ProcessingJob, aba_job_id)
                failures += check(
                    "recovery CAS rejects a prior retry after failed-pending ABA reuse",
                    first_aba_admission.created
                    and second_aba_admission.created
                    and first_anchor is not None
                    and second_anchor is not None
                    and first_anchor != second_anchor
                    and not stale_changed
                    and after_stale_cas.status == "pending"
                    and after_stale_cas.started_at == second_anchor
                    and int(after_stale_cas.attempt_count or 0) == 4
                    and after_stale_cas.worker_pid is None,
                    (
                        f"first={first_anchor!r} second={second_anchor!r} "
                        f"changed={stale_changed} status={after_stale_cas.status!r} "
                        f"attempt={after_stale_cas.attempt_count!r} pid={after_stale_cas.worker_pid!r}"
                    ),
                )

                retried.worker_pid = 0
                retried.progress_json = '{"stage":"launching"}'
                db.session.commit()
                db.session.expire_all()
                reserved = db.session.get(ProcessingJob, job_id)
                reserved_reason = stale_reason(
                    reserved,
                    now=retry_anchor + timedelta(minutes=2),
                )
                failures += check(
                    "launch-reservation sentinel keeps the retry grace anchored to the current attempt",
                    reserved_reason is None,
                    f"reason={reserved_reason!r} worker_pid={reserved.worker_pid!r}",
                )

                late_reason = stale_reason(
                    reserved,
                    now=retry_anchor + ACTIVE_WITHOUT_WORKER_GRACE + timedelta(seconds=1),
                )
                failures += check(
                    "abandoned retried job still becomes recoverable after the new grace expires",
                    late_reason == "active job has no worker after launch grace period",
                    f"reason={late_reason!r}",
                )

                claimed, did_claim = _claim_pending_job(job_id, worker_pid=43120)
                claimed_anchor = _utc(claimed.started_at)
                failures += check(
                    "worker claim replaces the retry grace anchor with the actual running-attempt start",
                    did_claim
                    and claimed.status == "running"
                    and int(claimed.attempt_count or 0) == 3
                    and int(claimed.worker_pid or 0) == 43120
                    and claimed_anchor is not None
                    and claimed_anchor >= retry_anchor,
                    (
                        f"claimed={did_claim} status={claimed.status!r} attempt={claimed.attempt_count!r} "
                        f"worker_pid={claimed.worker_pid!r} started={claimed_anchor!r}"
                    ),
                )

                initial_pending = ProcessingJob(
                    project_id=project.id,
                    job_type="analyze_integrated",
                    status="pending",
                    progress_json='{"stage":"queued"}',
                    attempt_count=0,
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                    started_at=None,
                    worker_pid=None,
                )
                db.session.add(initial_pending)
                db.session.commit()
                db.session.expire_all()
                initial_pending = db.session.get(ProcessingJob, int(initial_pending.id))
                failures += check(
                    "first-time pending jobs still fall back to created_at for stale detection",
                    stale_reason(initial_pending) == "active job has no worker after launch grace period",
                )

                db.session.remove()
                db.engine.dispose()

        except Exception as exc:
            failures += check(
                "retry recovery grace smoke",
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
