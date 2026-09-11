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
            from services.processing_jobs import JobLeaseLost, execute_job, retry_failed_job
            import services.analyzer as analyzer_service
            import services.project_pipeline as project_pipeline_service
            import services.transcription_dispatch as transcription_dispatch_service

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
                failed_tr_id = failed_tr.id
                running_tr_id = running_tr.id

                original_dispatch_run = transcription_dispatch_service.run_transcription

                def fake_run_transcription(transcription_id, *, lease_check=None, result_write_guard=None):
                    if lease_check:
                        lease_check()
                    tr = db.session.get(Transcription, transcription_id)
                    iv = tr.media_file.interview
                    if result_write_guard is not None:
                        result_write_guard()
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
                    if lease_check:
                        lease_check()
                    return {"segment_count": 1, "word_count": 3}

                transcription_dispatch_service.run_transcription = fake_run_transcription
                try:
                    completed = execute_job(transcription_job_id, worker_pid=7001)
                finally:
                    transcription_dispatch_service.run_transcription = original_dispatch_run

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

                fallback_interview = Interview(project_id=project.id, status="pending")
                db.session.add(fallback_interview)
                db.session.flush()
                fallback_media = MediaFile(
                    interview_id=fallback_interview.id,
                    original_filename="fallback.wav",
                    stored_path="fallback.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(fallback_media)
                db.session.flush()
                fallback_tr = Transcription(
                    media_file_id=fallback_media.id,
                    whisper_model="fake",
                    language="ja",
                    status="pending",
                )
                db.session.add(fallback_tr)
                db.session.commit()
                fallback_tr_id = fallback_tr.id
                fallback_interview_id = fallback_interview.id

                saved_provider = transcription_dispatch_service.get_transcription_provider
                saved_fallback = transcription_dispatch_service.get_fallback_provider
                saved_openai = transcription_dispatch_service.run_openai_transcription
                saved_local = transcription_dispatch_service.run_local_whisper_transcription
                fallback_observation = {"segments_before_local": None}

                def fake_openai(target_id, **_kwargs):
                    target = db.session.get(Transcription, target_id)
                    db.session.add(Segment(
                        transcription_id=target.id,
                        interview_id=target.media_file.interview_id,
                        speaker_label="SPEAKER_00",
                        speaker_role="respondent",
                        text="openai partial",
                        seq=0,
                    ))
                    target.status = "error"
                    db.session.commit()
                    raise RuntimeError("openai chunk failure")

                def fake_local(target_id, **_kwargs):
                    target = db.session.get(Transcription, target_id)
                    existing_segments = Segment.query.filter_by(transcription_id=target_id).all()
                    fallback_observation["segments_before_local"] = len(existing_segments)
                    db.session.add(Segment(
                        transcription_id=target.id,
                        interview_id=target.media_file.interview_id,
                        speaker_label="SPEAKER_00",
                        speaker_role="respondent",
                        text="local canonical",
                        seq=0,
                    ))
                    target.status = "done"
                    target.media_file.interview.status = "transcribed"
                    db.session.commit()
                    return {"segment_count": 1, "word_count": 2}

                transcription_dispatch_service.get_transcription_provider = lambda: "openai"
                transcription_dispatch_service.get_fallback_provider = lambda: "local_whisper"
                transcription_dispatch_service.run_openai_transcription = fake_openai
                transcription_dispatch_service.run_local_whisper_transcription = fake_local
                try:
                    fallback_result = transcription_dispatch_service.run_transcription(fallback_tr_id)
                finally:
                    transcription_dispatch_service.get_transcription_provider = saved_provider
                    transcription_dispatch_service.get_fallback_provider = saved_fallback
                    transcription_dispatch_service.run_openai_transcription = saved_openai
                    transcription_dispatch_service.run_local_whisper_transcription = saved_local

                fallback_segments = Segment.query.filter_by(interview_id=fallback_interview_id).all()
                failures += check(
                    "fallback discards OpenAI partial segments before local Whisper",
                    fallback_observation["segments_before_local"] == 0
                    and fallback_result.get("fallback_discarded_partial_segment_count") == 1
                    and len(fallback_segments) == 1
                    and fallback_segments[0].text == "local canonical",
                    f"result={fallback_result} segments={[s.text for s in fallback_segments]}",
                )

                stale_interview = Interview(project_id=project.id, status="pending")
                db.session.add(stale_interview)
                db.session.flush()
                stale_media = MediaFile(
                    interview_id=stale_interview.id,
                    original_filename="stale.wav",
                    stored_path="stale.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(stale_media)
                db.session.flush()
                stale_tr = Transcription(
                    media_file_id=stale_media.id,
                    whisper_model="fake",
                    language="ja",
                    status="pending",
                )
                db.session.add(stale_tr)
                db.session.commit()
                stale_tr_id = stale_tr.id
                stale_interview_id = stale_interview.id

                saved_provider = transcription_dispatch_service.get_transcription_provider
                saved_openai = transcription_dispatch_service.run_openai_transcription
                lease_calls = {"count": 0}

                def stale_openai(target_id, **_kwargs):
                    target = db.session.get(Transcription, target_id)
                    db.session.add(Segment(
                        transcription_id=target.id,
                        interview_id=target.media_file.interview_id,
                        speaker_label="SPEAKER_00",
                        speaker_role="respondent",
                        text="stale result",
                        seq=0,
                    ))
                    target.status = "done"
                    target.media_file.interview.status = "transcribed"
                    db.session.commit()
                    return {"segment_count": 1}

                def lease_check():
                    lease_calls["count"] += 1
                    if lease_calls["count"] >= 2:
                        raise JobLeaseLost("simulated lease loss")

                transcription_dispatch_service.get_transcription_provider = lambda: "openai"
                transcription_dispatch_service.run_openai_transcription = stale_openai
                stale_raised = False
                try:
                    transcription_dispatch_service.run_transcription(
                        stale_tr_id,
                        lease_check=lease_check,
                    )
                except JobLeaseLost:
                    stale_raised = True
                finally:
                    transcription_dispatch_service.get_transcription_provider = saved_provider
                    transcription_dispatch_service.run_openai_transcription = saved_openai

                stale_after = db.session.get(Transcription, stale_tr_id)
                stale_segments = Segment.query.filter_by(transcription_id=stale_tr_id).all()
                stale_iv_after = db.session.get(Interview, stale_interview_id)
                failures += check(
                    "lease-lost transcription result is invalidated",
                    stale_raised
                    and stale_after.status == "error"
                    and len(stale_segments) == 0
                    and stale_iv_after.status == "pending",
                    f"tr={stale_after.status} segments={len(stale_segments)} interview={stale_iv_after.status}",
                )

                pipeline_project = Project(name="Pipeline transcription retry")
                db.session.add(pipeline_project)
                db.session.flush()
                pipeline_interview = Interview(project_id=pipeline_project.id, status="pending")
                db.session.add(pipeline_interview)
                db.session.flush()
                pipeline_media = MediaFile(
                    interview_id=pipeline_interview.id,
                    original_filename="pipeline.wav",
                    stored_path="pipeline.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(pipeline_media)
                db.session.flush()
                pipeline_old_tr = Transcription(
                    media_file_id=pipeline_media.id,
                    whisper_model="fake",
                    language="ja",
                    status="error",
                    error_message="old partial",
                )
                db.session.add(pipeline_old_tr)
                db.session.flush()
                db.session.add(Segment(
                    transcription_id=pipeline_old_tr.id,
                    interview_id=pipeline_interview.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    text="pipeline partial",
                    seq=0,
                ))
                pipeline_job = ProcessingJob(
                    project_id=pipeline_project.id,
                    job_type="project_pipeline",
                    status="pending",
                )
                db.session.add(pipeline_job)
                db.session.commit()
                pipeline_job_id = pipeline_job.id
                pipeline_interview_id = pipeline_interview.id

                saved_pipeline_run = project_pipeline_service.run_transcription

                def fake_pipeline_transcription(target_id, *, lease_check=None, result_write_guard=None):
                    if lease_check:
                        lease_check()
                    target = db.session.get(Transcription, target_id)
                    existing_segments = Segment.query.filter_by(interview_id=target.media_file.interview_id).all()
                    if any(seg.text == "pipeline partial" for seg in existing_segments):
                        raise AssertionError("pipeline partial segment survived retry cleanup")
                    if result_write_guard is not None:
                        result_write_guard()
                    db.session.add(Segment(
                        transcription_id=target.id,
                        interview_id=target.media_file.interview_id,
                        speaker_label="SPEAKER_00",
                        speaker_role="respondent",
                        text="pipeline canonical",
                        seq=0,
                    ))
                    target.status = "done"
                    target.media_file.interview.status = "transcribed"
                    db.session.commit()
                    if lease_check:
                        lease_check()
                    return {"segment_count": 1}

                project_pipeline_service.run_transcription = fake_pipeline_transcription
                try:
                    pipeline_result_job = execute_job(pipeline_job_id, worker_pid=7004)
                finally:
                    project_pipeline_service.run_transcription = saved_pipeline_run

                pipeline_segments = Segment.query.filter_by(interview_id=pipeline_interview_id).all()
                failures += check(
                    "project pipeline retry removes old partial transcription segments",
                    all(seg.text != "pipeline partial" for seg in pipeline_segments)
                    and any(seg.text == "pipeline canonical" for seg in pipeline_segments),
                    f"job={pipeline_result_job.status} segments={[s.text for s in pipeline_segments]}",
                )

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

                def should_not_reanalyze(_interview_id, **_kwargs):
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

                def fake_new_analysis(target_interview_id, *, result_write_guard=None):
                    regen_calls["count"] += 1
                    if result_write_guard is not None:
                        result_write_guard()
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
