from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from models import db
from models.processing_job import ProcessingJob
from services.job_recovery import mark_job_failed, stale_reason


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly fail one stuck processing job so it can be retried"
    )
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--apply", action="store_true", help="write the recovery state")
    parser.add_argument("--yes", action="store_true", help="confirm the write when --apply is used")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        job = db.session.get(ProcessingJob, args.job_id)
        if not job:
            print(f"job_id={args.job_id} not found")
            return 1

        payload = job.to_dict()
        payload["stale_reason"] = stale_reason(job)
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))

        if not args.apply:
            print("Validation only. No data changed.")
            return 0
        if not args.yes:
            print("Refusing write: --apply requires --yes.")
            return 2
        if job.status not in {"pending", "running"}:
            print(f"No change: job is already terminal ({job.status}).")
            return 0

        mark_job_failed(job, "operator explicitly recovered active job")
        print(f"job_id={job.id} marked failed; retry is now available")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
