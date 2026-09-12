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
    created = _parse_dt(row.get("created_at"))
    if not row.get("worker_pid") and created:
        if (datetime.now(timezone.utc) - created).total_seconds() > 300:
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


def _apply_recovery(job_id: int) -> int:
    from app import create_app
    from models import db
    from models.processing_job import ProcessingJob
    from services.job_recovery import mark_job_failed

    # Explicit recovery is a live-database write and create_app() may itself run
    # idempotent migrations. This nested runtime acquisition stays in the same
    # shared mode when main() already holds the reader reservation.
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
                mark_job_failed(job, "operator explicitly recovered active job")
                print(f"job_id={job.id} marked failed; retry is now available")
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
            return _apply_recovery(args.job_id)
    except RuntimeLockError as exc:
        print(f"Refusing job inspection while maintenance is active: {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
