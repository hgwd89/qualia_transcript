import sys
import tempfile
from datetime import datetime, timezone
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

    with tempfile.TemporaryDirectory(prefix="qualia_semantic_job_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'semantic.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            import numpy as np

            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview
            from models.participant import Participant
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment
            from services.job_admission import admit_processing_job, admit_retry_job
            from services.processing_jobs import execute_job
            from services.processing_result_guard import find_completed_result_for_active_job
            import services.processing_jobs as processing_jobs
            import services.semantic_analysis as semantic_analysis

            readiness_source = (repo_root / "scripts" / "audit_production_readiness_v2.py").read_text(encoding="utf-8")

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Semantic durable job smoke")
                db.session.add(project)
                db.session.flush()
                participant = Participant(
                    project_id=project.id,
                    participant_code="P01",
                    display_name="Participant 1",
                )
                db.session.add(participant)
                db.session.flush()
                interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    status="mapped",
                )
                db.session.add(interview)
                db.session.flush()
                db.session.add(Segment(
                    interview_id=interview.id,
                    participant_id=participant.id,
                    speaker_label="SPEAKER_01",
                    speaker_role="respondent",
                    text="安心して毎日使えることが一番大事だと思います。",
                    start_sec=0.0,
                    end_sec=3.0,
                    seq=0,
                ))
                db.session.commit()

                provider_called = {"value": False}
                original_embed = semantic_analysis.embed_fragments
                original_cluster = semantic_analysis.cluster_embeddings
                try:
                    def forbidden_embed(_fragments):
                        provider_called["value"] = True
                        raise AssertionError("provider work must not start")

                    semantic_analysis.embed_fragments = forbidden_embed
                    raised = False
                    try:
                        semantic_analysis.run_semantic_cluster_analysis(
                            interview.id,
                            save=True,
                            no_ai=True,
                        )
                    except RuntimeError as exc:
                        raised = "durable result-write guard" in str(exc)
                    failures += check(
                        "unguarded semantic save is rejected before provider work",
                        raised and not provider_called["value"],
                        f"raised={raised} provider_called={provider_called['value']}",
                    )
                finally:
                    semantic_analysis.embed_fragments = original_embed

                admission = admit_processing_job(
                    project.id,
                    "analyze_semantic",
                    interview.id,
                    request_payload={"max_segments": 1, "no_ai": True},
                )
                durable_job = db.session.get(ProcessingJob, int(admission.job_id))
                failures += check(
                    "semantic analysis is admitted with durable request parameters",
                    admission.created
                    and admission.job_id is not None
                    and admission.error is None
                    and durable_job.to_dict().get("request") == {"max_segments": 1, "no_ai": True},
                    f"admission={admission} job={durable_job.to_dict()}",
                )
                failures += check(
                    "semantic job type is accepted by readiness scope audit",
                    '"analyze_semantic": (True, False)' in readiness_source,
                    "rule source present=" + str('"analyze_semantic": (True, False)' in readiness_source),
                )

                duplicate_same_request = admit_processing_job(
                    project.id,
                    "analyze_semantic",
                    interview.id,
                    request_payload={"max_segments": 1, "no_ai": True},
                )
                failures += check(
                    "same semantic scope and request deduplicates",
                    duplicate_same_request.job_id == admission.job_id
                    and not duplicate_same_request.created
                    and duplicate_same_request.conflict_job_id is None,
                    str(duplicate_same_request),
                )

                mismatched_request = admit_processing_job(
                    project.id,
                    "analyze_semantic",
                    interview.id,
                    request_payload={"max_segments": 2, "no_ai": True},
                )
                failures += check(
                    "same semantic scope with different request is a conflict",
                    mismatched_request.conflict_job_id == admission.job_id,
                    str(mismatched_request),
                )

                conflicting = admit_processing_job(project.id, "analyze", interview.id)
                failures += check(
                    "semantic job participates in canonical interview conflict policy",
                    conflicting.conflict_job_id == admission.job_id,
                    str(conflicting),
                )

                semantic_analysis.embed_fragments = lambda fragments: np.ones(
                    (len(fragments), 3), dtype=np.float32
                )
                semantic_analysis.cluster_embeddings = lambda embeddings: [0] * int(embeddings.shape[0])
                try:
                    completed = execute_job(
                        int(admission.job_id),
                        worker_pid=4242,
                    )
                finally:
                    semantic_analysis.embed_fragments = original_embed
                    semantic_analysis.cluster_embeddings = original_cluster

                semantic_row = (
                    AIAnalysis.query
                    .filter_by(
                        project_id=project.id,
                        interview_id=interview.id,
                        analysis_type="semantic_clusters",
                    )
                    .order_by(AIAnalysis.id.desc())
                    .first()
                )
                failures += check(
                    "durable semantic handler restores persisted request parameters",
                    completed.status == "succeeded"
                    and semantic_row is not None
                    and semantic_row.model_used.endswith("no-ai-summary")
                    and completed.to_dict().get("request") == {"max_segments": 1, "no_ai": True},
                    f"job={completed.to_dict()} analysis_id={getattr(semantic_row, 'id', None)} model={getattr(semantic_row, 'model_used', None)}",
                )

                retry_source = ProcessingJob(
                    project_id=project.id,
                    interview_id=interview.id,
                    job_type="analyze_semantic",
                    status="failed",
                    request_json='{"max_segments":3,"no_ai":true}',
                    error_message="retry smoke",
                )
                db.session.add(retry_source)
                db.session.commit()
                retry_admission = admit_retry_job(retry_source.id)
                retried = db.session.get(ProcessingJob, retry_source.id)
                failures += check(
                    "failed semantic retry preserves durable request parameters",
                    retry_admission.created
                    and retried.status == "pending"
                    and retried.to_dict().get("request") == {"max_segments": 3, "no_ai": True},
                    f"admission={retry_admission} job={retried.to_dict()}",
                )
                retried.status = "failed"
                retried.finished_at = datetime.now(timezone.utc)
                db.session.commit()

                recovery_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=interview.id,
                    job_type="analyze_semantic",
                    status="running",
                    attempt_count=1,
                    worker_pid=999999,
                    started_at=datetime.now(timezone.utc),
                )
                db.session.add(recovery_job)
                db.session.flush()
                recovered_analysis = AIAnalysis(
                    project_id=project.id,
                    interview_id=interview.id,
                    analysis_type="semantic_clusters",
                    title="Recovered semantic",
                    summary_text="done",
                    content_json="{}",
                    model_used="smoke",
                    created_at=datetime.now(timezone.utc),
                )
                db.session.add(recovered_analysis)
                db.session.commit()
                recovery_result = find_completed_result_for_active_job(recovery_job)
                failures += check(
                    "semantic crash-window result is recoverable for current attempt",
                    bool(
                        recovery_result
                        and recovery_result.get("analysis_id") == recovered_analysis.id
                        and recovery_result.get("recovered_committed_result") is True
                    ),
                    str(recovery_result),
                )

                empty_interview = Interview(
                    project_id=project.id,
                    participant_id=participant.id,
                    status="mapped",
                )
                db.session.add(empty_interview)
                db.session.commit()
                empty_admission = admit_processing_job(
                    project.id,
                    "analyze_semantic",
                    empty_interview.id,
                    request_payload={"max_segments": None, "no_ai": True},
                )
                empty_result = execute_job(int(empty_admission.job_id), worker_pid=5252)
                failures += check(
                    "semantic empty-input failure preserves actionable diagnostics",
                    empty_result.status == "failed"
                    and "no_fragments_after_filter" in str(empty_result.error_message)
                    and "candidate_segment_count" in str(empty_result.error_message),
                    str(empty_result.error_message),
                )

                active = ProcessingJob(
                    project_id=project.id,
                    interview_id=interview.id,
                    job_type="analyze_semantic",
                    status="pending",
                )
                db.session.add(active)
                db.session.commit()
                client = app.test_client()
                segment = Segment.query.filter_by(interview_id=interview.id).first()
                response = client.post(
                    f"/interviews/{interview.id}/segments/{segment.id}/role",
                    json={"speaker_role": "observer"},
                )
                failures += check(
                    "active semantic job fences canonical segment-role input edits",
                    response.status_code == 409,
                    f"status={response.status_code} body={response.get_json(silent=True)}",
                )

                db.session.remove()
                db.engine.dispose()

        finally:
            config.DATABASE_URI = original["DATABASE_URI"]
            config.UPLOAD_DIR = original["UPLOAD_DIR"]
            config.OUTPUT_DIR = original["OUTPUT_DIR"]

    print("\nSummary:", "PASS" if failures == 0 else f"FAIL ({failures})")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
