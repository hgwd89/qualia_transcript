import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{suffix}")
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
    }
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_job_admission_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'admission.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.processing_job import ProcessingJob
            from models.project import Project
            from services.job_admission import admit_processing_job, admit_retry_job

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Atomic admission smoke")
                db.session.add(project)
                db.session.flush()
                iv1 = Interview(project_id=project.id, status="transcribed")
                iv2 = Interview(project_id=project.id, status="transcribed")
                db.session.add_all([iv1, iv2])
                db.session.commit()
                project_id = project.id
                iv1_id = iv1.id
                iv2_id = iv2.id

            def concurrent_admit(specs):
                barrier = threading.Barrier(len(specs))

                def worker(spec):
                    job_type, interview_id = spec
                    with app.app_context():
                        barrier.wait(timeout=10)
                        admission = admit_processing_job(project_id, job_type, interview_id)
                        return {
                            "job_id": admission.job_id,
                            "created": admission.created,
                            "conflict_job_id": admission.conflict_job_id,
                            "error": admission.error,
                        }

                with ThreadPoolExecutor(max_workers=len(specs)) as pool:
                    return list(pool.map(worker, specs))

            def finish_active_jobs():
                with app.app_context():
                    for job in ProcessingJob.query.filter_by(project_id=project_id).filter(
                        ProcessingJob.status.in_(("pending", "running"))
                    ).all():
                        job.status = "succeeded"
                        job.worker_pid = None
                        job.finished_at = datetime.now(timezone.utc)
                    db.session.commit()

            same = concurrent_admit([("map", iv1_id), ("map", iv1_id)])
            same_ids = {row["job_id"] for row in same if row["job_id"] is not None}
            failures += check(
                "same-scope concurrent admission deduplicates to one job",
                len(same_ids) == 1
                and sum(1 for row in same if row["created"]) == 1
                and sum(1 for row in same if not row["created"] and row["conflict_job_id"] is None and row["error"] is None) == 1,
                str(same),
            )
            with app.app_context():
                active = ProcessingJob.query.filter_by(project_id=project_id, interview_id=iv1_id, status="pending").all()
                failures += check("same-scope race leaves one active row", len(active) == 1, str([j.id for j in active]))
            finish_active_jobs()

            interview_conflict = concurrent_admit([("map", iv1_id), ("analyze", iv1_id)])
            created_rows = [row for row in interview_conflict if row["created"]]
            conflict_rows = [row for row in interview_conflict if row["conflict_job_id"]]
            failures += check(
                "different job types on one interview serialize admission",
                len(created_rows) == 1
                and len(conflict_rows) == 1
                and conflict_rows[0]["conflict_job_id"] == created_rows[0]["job_id"],
                str(interview_conflict),
            )
            with app.app_context():
                active = ProcessingJob.query.filter_by(project_id=project_id, interview_id=iv1_id, status="pending").all()
                failures += check("interview conflict race leaves one active row", len(active) == 1, str([j.id for j in active]))
            finish_active_jobs()

            project_conflict = concurrent_admit([("project_pipeline", None), ("map", iv2_id)])
            created_rows = [row for row in project_conflict if row["created"]]
            conflict_rows = [row for row in project_conflict if row["conflict_job_id"]]
            failures += check(
                "project pipeline and interview job serialize admission",
                len(created_rows) == 1
                and len(conflict_rows) == 1
                and conflict_rows[0]["conflict_job_id"] == created_rows[0]["job_id"],
                str(project_conflict),
            )
            with app.app_context():
                active = ProcessingJob.query.filter_by(project_id=project_id, status="pending").all()
                failures += check("project conflict race leaves one active row", len(active) == 1, str([j.id for j in active]))
            finish_active_jobs()

            with app.app_context():
                failed_job = ProcessingJob(
                    project_id=project_id,
                    interview_id=iv1_id,
                    job_type="analyze",
                    status="failed",
                    error_message="old failure",
                )
                db.session.add(failed_job)
                db.session.commit()
                failed_job_id = failed_job.id

            retry_barrier = threading.Barrier(2)

            def retry_worker(_):
                with app.app_context():
                    retry_barrier.wait(timeout=10)
                    admission = admit_retry_job(failed_job_id)
                    return {
                        "job_id": admission.job_id,
                        "created": admission.created,
                        "conflict_job_id": admission.conflict_job_id,
                        "error": admission.error,
                    }

            with ThreadPoolExecutor(max_workers=2) as pool:
                retry_rows = list(pool.map(retry_worker, range(2)))
            failures += check(
                "concurrent retry requeues failed job once",
                sum(1 for row in retry_rows if row["created"]) == 1
                and sum(1 for row in retry_rows if row["error"] == "only failed jobs can be retried") == 1,
                str(retry_rows),
            )
            with app.app_context():
                retried = db.session.get(ProcessingJob, failed_job_id)
                failures += check(
                    "retry race leaves target pending exactly once",
                    retried.status == "pending" and retried.error_message is None,
                    str(retried.to_dict()),
                )
                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check("job admission smoke", False, f"{type(exc).__name__}: {exc}")
        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
