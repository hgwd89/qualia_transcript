from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from services.processing_jobs import execute_job
from services.runtime_lock import runtime_lock


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute one durable Qualia processing job")
    parser.add_argument("--job-id", type=int, required=True)
    args = parser.parse_args()

    # Detached workers can outlive the Flask process. Keep a shared runtime lock
    # for the worker lifetime so backup/applied restore cannot start while this
    # process can still write database, upload, output, or raw-snapshot state.
    with runtime_lock("worker"):
        app = create_app()
        with app.app_context():
            job = execute_job(args.job_id, worker_pid=os.getpid())
            print(f"job_id={job.id} status={job.status}")
            if job.error_message:
                print(job.error_message)
            return 0 if job.status == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
