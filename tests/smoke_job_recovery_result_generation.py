from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
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

    with tempfile.TemporaryDirectory(prefix="qualia_job_result_generation_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'result-generation.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            import services.job_recovery as recovery
            from services.processing_result_guard import (
                find_completed_analysis_for_job,
                find_completed_mapping_count_for_job,
            )

            app = create_app()
            app.config["TESTING"] = True
            now = datetime.now(timezone.utc)
            dead_pid = 424242

            with app.app_context():
                project = Project(name="Job result generation fence")
                db.session.add(project)
                db.session.flush()

                current_analysis_interview = Interview(project_id=project.id, status="analyzed")
                old_analysis_interview = Interview(project_id=project.id, status="analyzed")
                mapping_interview = Interview(project_id=project.id, status="mapped")
                db.session.add_all([
                    current_analysis_interview,
                    old_analysis_interview,
                    mapping_interview,
                ])
                db.session.flush()

                current_analysis_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=current_analysis_interview.id,
                    job_type="analyze",
                    status="running",
                    attempt_count=2,
                    worker_pid=dead_pid,
                    created_at=now - timedelta(days=1),
                    started_at=now - timedelta(minutes=2),
                )
                old_analysis_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=old_analysis_interview.id,
                    job_type="analyze",
                    status="running",
                    attempt_count=2,
                    worker_pid=dead_pid,
                    created_at=now - timedelta(days=1),
                    started_at=now - timedelta(minutes=1),
                )
                mapping_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=mapping_interview.id,
                    job_type="map",
                    status="running",
                    attempt_count=3,
                    worker_pid=dead_pid,
                    created_at=now - timedelta(days=2),
                    started_at=now - timedelta(minutes=2),
                )
                db.session.add_all([current_analysis_job, old_analysis_job, mapping_job])
                db.session.flush()

                current_analysis = AIAnalysis(
                    project_id=project.id,
                    interview_id=current_analysis_interview.id,
                    analysis_type="per_participant",
                    title="current attempt",
                    created_at=now - timedelta(minutes=1),
                )
                old_analysis = AIAnalysis(
                    project_id=project.id,
                    interview_id=old_analysis_interview.id,
                    analysis_type="per_participant",
                    title="older attempt",
                    created_at=now - timedelta(hours=2),
                )
                segment = Segment(
                    interview_id=mapping_interview.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="mapping evidence",
                    seq=1,
                    created_at=now - timedelta(hours=1),
                )
                db.session.add_all([current_analysis, old_analysis, segment])
                db.session.flush()
                mapping = UtteranceMapping(
                    segment_id=segment.id,
                    mapped_by="ai",
                    confidence=1.0,
                    is_unclassified=False,
                    created_at=now - timedelta(minutes=1),
                )
                db.session.add(mapping)
                db.session.commit()

                current_analysis_job_id = int(current_analysis_job.id)
                old_analysis_job_id = int(old_analysis_job.id)
                mapping_job_id = int(mapping_job.id)
                current_analysis_id = int(current_analysis.id)

                failures += check(
                    "analysis reconciliation uses current attempt started_at",
                    find_completed_analysis_for_job(current_analysis_job) is not None
                    and find_completed_analysis_for_job(old_analysis_job) is None,
                )
                failures += check(
                    "mapping reconciliation uses current attempt started_at",
                    find_completed_mapping_count_for_job(mapping_job) == 1,
                )

                recovered = recovery.recover_stale_jobs(
                    project_id=project.id,
                    pid_checker=lambda _pid: False,
                )
                recovered_ids = {int(job.id) for job in recovered}
                db.session.expire_all()

                current_after = db.session.get(ProcessingJob, current_analysis_job_id)
                old_after = db.session.get(ProcessingJob, old_analysis_job_id)
                mapping_after = db.session.get(ProcessingJob, mapping_job_id)

                current_result = json.loads(current_after.result_json or "{}")
                mapping_result = json.loads(mapping_after.result_json or "{}")

                failures += check(
                    "dead worker with current-attempt committed analysis recovers succeeded",
                    current_analysis_job_id in recovered_ids
                    and current_after.status == "succeeded"
                    and current_result.get("analysis_id") == current_analysis_id
                    and current_result.get("recovered_committed_result") is True
                    and current_after.error_message is None,
                    f"status={current_after.status!r} result={current_result!r}",
                )
                failures += check(
                    "older-attempt analysis is not adopted by retry generation",
                    old_analysis_job_id in recovered_ids
                    and old_after.status == "failed"
                    and "worker process is no longer running" in str(old_after.error_message or ""),
                    f"status={old_after.status!r} error={old_after.error_message!r}",
                )
                failures += check(
                    "dead worker with current-attempt committed mapping recovers succeeded",
                    mapping_job_id in recovered_ids
                    and mapping_after.status == "succeeded"
                    and mapping_result.get("mapped_count") == 1
                    and mapping_result.get("recovered_committed_result") is True,
                    f"status={mapping_after.status!r} result={mapping_result!r}",
                )

                db.session.remove()
                db.engine.dispose()

        except Exception as exc:
            failures += check(
                "job recovery result-generation smoke",
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

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
