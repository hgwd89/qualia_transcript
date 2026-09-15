from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from services.job_recovery import worker_pid_liveness
from services.runtime_lock import RuntimeLockError, runtime_lock


def _db_path() -> Path:
    uri = str(config.DATABASE_URI)
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise ValueError("processing job recovery currently supports sqlite:/// only")
    raw = unquote(uri[len(prefix):])
    path = Path(raw)
    if not path.is_absolute():
        path = Path(config.BASE_DIR) / path
    return path.resolve()


def _read_job(job_id: int) -> dict | None:
    path = _db_path()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        row = con.execute(
            """
            SELECT id, project_id, interview_id, job_type, status,
                   progress_json, result_json, error_message, attempt_count,
                   worker_pid, created_at, started_at, finished_at
            FROM processing_jobs
            WHERE id=?
            """,
            (job_id,),
        ).fetchone()
        if not row:
            return None
        return dict(row)
    finally:
        con.close()


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _dt_token(value) -> str | None:
    parsed = _parse_dt(value)
    return parsed.isoformat() if parsed is not None else None


def _recovery_generation_token_from_row(row: dict) -> dict:
    """Capture the exact durable active generation the operator inspected."""
    worker_pid = row.get("worker_pid")
    return {
        "status": str(row.get("status") or ""),
        "attempt_count": int(row.get("attempt_count") or 0),
        "worker_pid": int(worker_pid) if worker_pid is not None else None,
        "created_at": _dt_token(row.get("created_at")),
        "started_at": _dt_token(row.get("started_at")),
    }


def _recovery_generation_token_from_job(job) -> dict:
    worker_pid = getattr(job, "worker_pid", None)
    return {
        "status": str(getattr(job, "status", "") or ""),
        "attempt_count": int(getattr(job, "attempt_count", 0) or 0),
        "worker_pid": int(worker_pid) if worker_pid is not None else None,
        "created_at": _dt_token(getattr(job, "created_at", None)),
        "started_at": _dt_token(getattr(job, "started_at", None)),
    }


def _pid_liveness_from_row(row: dict) -> bool | None:
    if row.get("status") not in {"pending", "running"}:
        return None
    pid = row.get("worker_pid")
    if not pid:
        return None
    return worker_pid_liveness(pid)


def _stale_reason_from_row(row: dict) -> str | None:
    if row.get("status") not in {"pending", "running"}:
        return None

    # Match services.job_recovery.stale_reason(): retries reuse the durable row,
    # so created_at is historical identity while started_at anchors the current
    # admitted/claimed attempt. First-time pending rows fall back to created_at.
    active_since = _parse_dt(row.get("started_at")) or _parse_dt(row.get("created_at"))
    if not row.get("worker_pid") and active_since:
        if (datetime.now(timezone.utc) - active_since).total_seconds() > 300:
            return "active job has no worker after launch grace period"
        return None

    if row.get("worker_pid") and _pid_liveness_from_row(row) is False:
        return "worker process is no longer running"
    return None


def _display_payload(row: dict) -> dict:
    payload = dict(row)
    for field in ("progress_json", "result_json"):
        raw = payload.get(field)
        if raw:
            try:
                payload[field[:-5]] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                payload[field[:-5]] = None
        payload.pop(field, None)

    pid_liveness = _pid_liveness_from_row(row)
    payload["worker_pid_alive"] = pid_liveness
    payload["automatic_recovery_reason"] = _stale_reason_from_row(row)
    payload["recovery_generation"] = _recovery_generation_token_from_row(row)

    if row.get("status") in {"pending", "running"} and row.get("worker_pid"):
        if pid_liveness is True:
            payload["operator_note"] = (
                "Worker PID is currently alive. Do not force-recover unless you have independently "
                "confirmed that this PID is not the Qualia worker for this job."
            )
        elif pid_liveness is False:
            payload["operator_note"] = (
                "Worker PID is no longer alive. Normal status polling/conflict checks should auto-recover this job."
            )
        else:
            payload["operator_note"] = (
                "Worker PID liveness is inconclusive. Confirm the worker stopped before using --apply --yes."
            )
    else:
        payload["operator_note"] = None
    return payload


def _apply_recovery(job_id: int, expected_generation: dict) -> int:
    from app import create_app
    from models import db
    from models.processing_job import ProcessingJob
    from services.job_recovery import mark_job_failed_if_unchanged

    # Explicit recovery is a live-database write and create_app() may itself run
    # idempotent migrations. The caller keeps the reader reservation, while this
    # nested worker reservation excludes maintenance. Workers remain concurrent,
    # so both the inspected generation token and the service CAS are required.
    try:
        with runtime_lock("worker"):
            app = create_app()
            with app.app_context():
                job = db.session.get(ProcessingJob, job_id)
                if not job:
                    print(f"job_id={job_id} not found")
                    return 1
                if job.status not in {"pending", "running"}:
                    print(f"No change: job is already terminal ({job.status}).")
                    return 0

                current_generation = _recovery_generation_token_from_job(job)
                if current_generation != expected_generation:
                    print("Refusing recovery write: active job generation changed after inspection.")
                    print(json.dumps({
                        "inspected_generation": expected_generation,
                        "current_generation": current_generation,
                    }, ensure_ascii=False, indent=2))
                    return 4

                current, changed = mark_job_failed_if_unchanged(
                    job,
                    "operator explicitly recovered active job",
                )
                if not changed:
                    print("Refusing recovery write: active job generation changed during recovery commit.")
                    print(json.dumps({
                        "inspected_generation": expected_generation,
                        "current_generation": _recovery_generation_token_from_job(current),
                    }, ensure_ascii=False, indent=2))
                    return 4

                print(f"job_id={current.id} marked failed; retry is now available")
                return 0
    except RuntimeLockError as exc:
        print(f"Refusing recovery write while maintenance is active: {exc}")
        return 3


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly fail one stuck processing job so it can be retried"
    )
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="write the recovery state")
    parser.add_argument("--yes", action="store_true", help="confirm the write when --apply is used")
    args = parser.parse_args()

    try:
        with runtime_lock("reader"):
            try:
                row = _read_job(args.job_id)
            except Exception as exc:
                print(f"inspection failed: {type(exc).__name__}: {exc}")
                return 1
            if not row:
                print(f"job_id={args.job_id} not found")
                return 1

            print(json.dumps(_display_payload(row), ensure_ascii=False, indent=2, default=str))

            if not args.apply:
                print("Validation only. Database opened read-only; no job state changed.")
                return 0
            if not args.yes:
                print("Refusing write: --apply requires --yes.")
                return 2
            inspected_generation = _recovery_generation_token_from_row(row)
            return _apply_recovery(args.job_id, inspected_generation)
    except RuntimeLockError as exc:
        print(f"Refusing job inspection while maintenance is active: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
