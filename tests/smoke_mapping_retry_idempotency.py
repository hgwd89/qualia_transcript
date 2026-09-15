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

    with tempfile.TemporaryDirectory(prefix="qualia_mapping_retry_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'mapping_retry.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment, UtteranceMapping, UtteranceMappingProvenance
            from services.mapping_source_provenance import (
                capture_mapping_source_provenance,
                serialize_mapping_source_provenance,
            )
            from services.processing_jobs import execute_job, retry_failed_job
            import services.mapper as mapper_service

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Mapping retry idempotency")
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Retry flow")
                db.session.add(flow)
                db.session.flush()
                section = InterviewFlowSection(flow_id=flow.id, title="Retry section", seq=0)
                db.session.add(section)
                db.session.flush()
                question = InterviewFlowQuestion(
                    section_id=section.id,
                    question_code="Q1",
                    question_text="Retry question",
                    seq=0,
                )
                db.session.add(question)
                db.session.flush()
                interview = Interview(project_id=project.id, flow_id=flow.id, status="mapped")
                db.session.add(interview)
                db.session.flush()
                segment = Segment(
                    interview_id=interview.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    text="answer",
                    seq=0,
                )
                db.session.add(segment)
                db.session.flush()

                job_created = datetime.now(timezone.utc) - timedelta(seconds=5)
                crashed_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=interview.id,
                    job_type="map",
                    status="failed",
                    attempt_count=1,
                    error_message="worker died after mapping commit",
                    created_at=job_created,
                )
                db.session.add(crashed_job)
                db.session.flush()
                mapping = UtteranceMapping(
                    segment_id=segment.id,
                    question_id=None,
                    mapped_by="ai",
                    confidence=0.4,
                    is_unclassified=True,
                    created_at=datetime.now(timezone.utc),
                )
                db.session.add(mapping)
                db.session.flush()
                db.session.add(UtteranceMappingProvenance(
                    mapping_id=mapping.id,
                    source_provenance_json=serialize_mapping_source_provenance(
                        capture_mapping_source_provenance(interview.id)
                    ),
                ))
                db.session.commit()
                crashed_job_id = crashed_job.id

                retry_failed_job(crashed_job)
                original_run_mapping = mapper_service.run_mapping
                calls = {"count": 0}

                def should_not_remap(*_args, **_kwargs):
                    calls["count"] += 1
                    raise AssertionError("mapping committed in crash window should be reused")

                mapper_service.run_mapping = should_not_remap
                try:
                    completed = execute_job(crashed_job_id, worker_pid=7301)
                finally:
                    mapper_service.run_mapping = original_run_mapping

                result = completed.to_dict().get("result") or {}
                failures += check(
                    "retry reuses provenance-valid mapping committed before worker crash",
                    completed.status == "succeeded"
                    and result.get("mapped_count") == 1
                    and result.get("already_done") is True
                    and calls["count"] == 0,
                    f"job={completed.to_dict()} calls={calls['count']}",
                )

                # A later intentional map job must not reuse mappings that predate
                # the new durable job.
                intentional_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=interview.id,
                    job_type="map",
                    status="pending",
                    created_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                )
                db.session.add(intentional_job)
                db.session.commit()
                intentional_id = intentional_job.id
                remap_calls = {"count": 0}

                def fake_remap(_interview_id, *, result_write_guard=None):
                    remap_calls["count"] += 1
                    return 9

                mapper_service.run_mapping = fake_remap
                try:
                    regenerated = execute_job(intentional_id, worker_pid=7302)
                finally:
                    mapper_service.run_mapping = original_run_mapping

                regenerated_result = regenerated.to_dict().get("result") or {}
                failures += check(
                    "intentional later map job still regenerates mapping",
                    regenerated.status == "succeeded"
                    and regenerated_result.get("mapped_count") == 9
                    and regenerated_result.get("already_done") is not True
                    and remap_calls["count"] == 1,
                    f"job={regenerated.to_dict()} calls={remap_calls['count']}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check("mapping retry idempotency smoke", False, f"{type(exc).__name__}: {exc}")
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
