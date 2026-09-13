import argparse
import json
import os
import sys

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from app import create_app
from models import db
from models.interview import Interview
from models.processing_job import ProcessingJob
from services.job_admission import admit_processing_job
from services.processing_jobs import execute_job
from services.runtime_lock import RuntimeLockError, runtime_lock
from services.semantic_analysis import run_semantic_cluster_analysis


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Semantic cluster analysis CLI")
    parser.add_argument("--interview-id", type=int, required=True, help="target interview id")
    parser.add_argument("--max-segments", type=int, default=None, help="max respondent segments after filtering")
    parser.add_argument("--no-ai", action="store_true", help="skip AI cluster summarization")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="do not save AIAnalysis (default)")
    mode.add_argument("--save", action="store_true", help="save AIAnalysis with analysis_type=semantic_clusters")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    save = bool(args.save)
    if not args.save and not args.dry_run:
        save = False  # default dry-run

    try:
        with runtime_lock("worker"):
            app = create_app()
            with app.app_context():
                if not save:
                    result = run_semantic_cluster_analysis(
                        interview_id=args.interview_id,
                        save=False,
                        max_segments=args.max_segments,
                        no_ai=bool(args.no_ai),
                    )
                else:
                    interview = db.session.get(Interview, int(args.interview_id))
                    if interview is None:
                        raise ValueError(f"interview_id={args.interview_id} not found")

                    admission = admit_processing_job(
                        int(interview.project_id),
                        "analyze_semantic",
                        int(interview.id),
                        request_payload={
                            "max_segments": args.max_segments,
                            "no_ai": bool(args.no_ai),
                        },
                    )
                    if admission.error:
                        result = {
                            "ok": False,
                            "error_type": "JobAdmissionError",
                            "error_message": admission.error,
                        }
                    elif admission.conflict_job_id is not None:
                        result = {
                            "ok": False,
                            "error_type": "JobConflict",
                            "error_message": "another processing job is active for this analysis scope",
                            "conflict_job_id": int(admission.conflict_job_id),
                        }
                    elif not admission.created:
                        job = db.session.get(ProcessingJob, int(admission.job_id))
                        result = {
                            "ok": False,
                            "in_progress": True,
                            "error_type": "JobAlreadyActive",
                            "error_message": "same-scope semantic analysis is still pending or running",
                            "job": job.to_dict() if job else None,
                        }
                    else:
                        completed = execute_job(
                            int(admission.job_id),
                            worker_pid=os.getpid(),
                        )
                        result = {
                            "ok": completed.status == "succeeded",
                            "job": completed.to_dict(),
                        }
    except RuntimeLockError as exc:
        print(json.dumps({
            "ok": False,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }, ensure_ascii=False))
        return 3
    except Exception as e:
        print(json.dumps({
            "ok": False,
            "error_type": type(e).__name__,
            "error_message": str(e),
        }, ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("in_progress"):
        return 2
    if not result.get("ok", False):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
