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

    with tempfile.TemporaryDirectory(prefix="qualia_result_idempotency_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'results.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.analysis import AIAnalysis
            from models.interview import Interview, MediaFile, Transcription
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment
            from services.processing_jobs import execute_job, retry_failed_job
            import services.analyzer as analyzer_service
            import services.transcription as transcription_service

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Result idempotency smoke")
                db.session.add(project)
                db.session.flush()

                transcription_interview = Interview(project_id=project.id, status="pending")
                db.session.add(transcription_interview)
                db.session.flush()
                media = MediaFile(
                    interview_id=transcription_interview.id,
                    original_filename="retry.wav",
                    stored_path="retry.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(media)
                db.session.flush()

                failed_tr = Transcription(
                    media_file_id=media.id,
                    whisper_model="fake",
                    language="ja",
                    status="error",
                    error_message="chunk 2 failed",
                )
                running_tr = Transcription(
                    media_file_id=media.id,
                    whisper_model="fake",
                    language="ja",
                    status="running",
                )
                db.session.add_all([failed_tr, running_tr])
                db.session.flush()
                db.session.add_all([
                    Segment(
                        transcription_id=failed_tr.id,
                        interview_id=transcription_interview.id,
                        speaker_label="SPEAKER_00",
                        speaker_role="respondent",
                        text="partial one",
                        seq=0,
                    ),
                    Segment(
                        transcription_id=running_tr.id,
                        interview_id=transcription_interview.id,
                        speaker_label="SPEAKER_00",
                        speaker_role="respondent",
                        text="partial two",
                        seq=1,
                    ),
                ])
                transcription_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=transcription_interview.id,
                    job_type="transcribe",
                    status="pending",
                )
                db.session.add(transcription_job)
                db.session.commit()
                transcription_job_id = transcription_job.id
                interview_id = transcription_interview.id
                media_id = media.id
                failed_tr_id = failed_tr.id
                running_tr_id = running_tr.id

                original_run_transcription = transcription_service.run_transcription

                def fake_run_transcription(transcription_id):
                    tr = db.session.get(Transcription, transcription_id)
                    iv = tr.media_file.interview
                    db.session.add(Segment(
                        transcription_id=tr.id,
                        interview_id=iv.id,
                        speaker_label="SPEAKER_00",
                        speaker_role="respondent",
                        text="canonical retry result",
                        seq=0,
                    ))
                    tr.status = "done"
                    tr.word_count = 3
                    tr.completed_at = datetime.now(timezone.utc)
                    iv.status = "transcribed"
                    db.session.commit()
                    return {"segment_count": 1, "word_count": 3}

                transcription_service.run_transcription = fake_run_transcription
                try:
                    completed = execute_job(transcription_job_id, worker_pid=7001)
                finally:
                    transcription_service.run_transcription = original_run_transcription

                result = completed.to_dict().get("result") or {}
                remaining_segments = Segment.query.filter_by(interview_id=interview_id).all()
                failures += check(
                    "retry removes partial transcription segments before canonical rerun",
                    completed.status == "succeeded"
                    and result.get("discarded_partial_segment_count") == 2
                    and len(remaining_segments) == 1
                    and remaining_segments[0].text == "canonical retry result",
                    f"result={result} segments={[s.text for s in remaining_segments]}",
                )
                failures += check(
                    "superseded incomplete transcription rows remain auditable",
                    db.session.get(Transcription, failed_tr_id).status == "error"
                    and db.session.get(Transcription, running_tr_id).status == "error"
                    and len(db.session.get(Transcription, failed_tr_id).segments) == 0
                    and len(db.session.get(Transcription, running_tr_id).segments) == 0,
                )

                # Crash window: analysis was committed, but durable job never
                # recorded success and later became failed/retryable.
                analysis_interview = Interview(project_id=project.id, status="mapped")
                db.session.add(analysis_interview)
                db.session.flush()
                crash_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=analysis_interview.id,
                    job_type="analyze",
                    status="failed",
                    attempt_count=1,
                    created_at=datetime.now(timezone.utc) - timedelta(seconds=5),
                    error_message="worker disappeared after analysis commit",
                )
                db.session.add(crash_job)
                db.session.flush()
                committed_analysis = AIAnalysis(
                    project_id=project.id,
                    interview_id=analysis_interview.id,
                    analysis_type="per_participant",
                    title="already committed",
                    summary_text="existing",
                    content_json='{"findings":[]}',
                    model_used="fake",
                    created_at=datetime.now(timezone.utc),
                )
                db.session.add(committed_analysis)
                analysis_interview.status = "analyzed"
                db.session.commit()
                crash_job_id = crash_job.id
                committed_analysis_id = committed_analysis.id

                retry_failed_job(crash_job)
                analyzer_calls = {"count": 0}
                original_analyze = analyzer_service.analyze_interview_summary

                def should_not_reanalyze(_interview_id):
                    analyzer_calls["count"] += 1
                    raise AssertionError("analysis should have been reused")

                analyzer_service.analyze_interview_summary = should_not_reanalyze
                try:
                    retried_analysis_job = execute_job(crash_job_id, worker_pid=7002)
                finally:
                    analyzer_service.analyze_interview_summary = original_analyze

                analysis_result = retried_analysis_job.to_dict().get("result") or {}
                failures += check(
                    "retry reuses analysis committed in prior crash window",
                    retried_analysis_job.status == "succeeded"
                    and analysis_result.get("analysis_id") == committed_analysis_id
                    and analysis_result.get("already_done") is True
                    and analyzer_calls["count"] == 0,
                    f"job={retried_analysis_job.to_dict()} calls={analyzer_calls['count']}",
                )

                # Intentional new analyze job must not reuse an analysis that
                # existed before the job itself was created.
                regen_interview = Interview(project_id=project.id, status="analyzed")
                db.session.add(regen_interview)
                db.session.flush()
                old_analysis = AIAnalysis(
                    project_id=project.id,
                    interview_id=regen_interview.id,
                    analysis_type="per_participant",
                    title="old analysis",
                    summary_text="old",
                    content_json='{"findings":[]}',
                    model_used="fake",
                    created_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                )
                db.session.add(old_analysis)
                db.session.commit()

                regen_job = ProcessingJob(
                    project_id=project.id,
                    interview_id=regen_interview.id,
                    job_type="analyze",
                    status="pending",
                    created_at=datetime.now(timezone.utc),
                )
                db.session.add(regen_job)
                db.session.commit()
                regen_job_id = regen_job.id
                regen_calls = {"count": 0}

                def fake_new_analysis(target_interview_id):
                    regen_calls["count"] += 1
                    analysis = AIAnalysis(
                        project_id=project.id,
                        interview_id=target_interview_id,
                        analysis_type="per_participant",
                        title="regenerated",
                        summary_text="new",
                        content_json='{"findings":[]}',
                        model_used="fake",
                    )
                    db.session.add(analysis)
                    db.session.commit()
                    return analysis

                analyzer_service.analyze_interview_summary = fake_new_analysis
                try:
                    regenerated_job = execute_job(regen_job_id, worker_pid=7003)
                finally:
                    analyzer_service.analyze_interview_summary = original_analyze

                regenerated_result = regenerated_job.to_dict().get("result") or {}
                failures += check(
                    "new analysis job still performs intentional regeneration",
                    regenerated_job.status == "succeeded"
                    and regen_calls["count"] == 1
                    and regenerated_result.get("analysis_id") != old_analysis.id
                    and regenerated_result.get("already_done") is not True,
                    f"result={regenerated_result} calls={regen_calls['count']}",
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check("processing result idempotency smoke", False, f"{type(exc).__name__}: {exc}")
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
