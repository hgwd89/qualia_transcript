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


def _stale_reason_from_row(row: dict) -> str | None:
    if row.get("status") not in {"pending", "running"}:
        return None
    now = datetime.now(timezone.utc)
    created = _parse_dt(row.get("created_at"))
    started = _parse_dt(row.get("started_at"))

    if not row.get("worker_pid") and created and (now - created).total_seconds() > 300:
        return "active job has no worker after launch grace period"

    reference = started or created
    if reference and (now - reference).total_seconds() > 43200:
        return "active job exceeded maximum runtime"
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
    payload["stale_reason"] = _stale_reason_from_row(row)
    return payload


def _apply_recovery(job_id: int) -> int:
    from app import create_app
    from models import db
    from models.processing_job import ProcessingJob
    from services.job_recovery import mark_job_failed

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly fail one stuck processing job so it can be retried"
    )
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="write the recovery state")
    parser.add_argument("--yes", action="store_true", help="confirm the write when --apply is used")
    args = parser.parse_args()

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


if __name__ == "__main__":
    raise SystemExit(main())
