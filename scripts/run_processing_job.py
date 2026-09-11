from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from services.processing_jobs import execute_job


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one durable Qualia processing job")
    parser.add_argument("--job-id", type=int, required=True)
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        job = execute_job(args.job_id)
        print(f"job_id={job.id} status={job.status}")
        if job.error_message:
            print(job.error_message)
        return 0 if job.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
