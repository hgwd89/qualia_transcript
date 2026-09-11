import sys
import tempfile
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

    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
    }

    with tempfile.TemporaryDirectory(prefix="qualia_pipeline_write_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'pipeline_fencing.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile, Transcription
            from models.interview_flow import InterviewFlow
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment
            from services.processing_jobs import JobLeaseLost
            import services.project_pipeline as pipeline_service

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                def running_job(project_id: int, pid: int) -> ProcessingJob:
                    job = ProcessingJob(
                        project_id=project_id,
                        job_type="project_pipeline",
                        status="running",
                        attempt_count=1,
                        worker_pid=pid,
                    )
                    db.session.add(job)
                    db.session.commit()
                    job._lease_attempt = 1
                    return job

                def supersede(job: ProcessingJob, pid: int) -> None:
                    (
                        ProcessingJob.query
                        .filter_by(id=job.id)
                        .update(
                            {
                                ProcessingJob.attempt_count: 2,
                                ProcessingJob.status: "running",
                                ProcessingJob.worker_pid: pid,
                            },
                            synchronize_session=False,
                        )
                    )
                    db.session.commit()

                # 1) flow_id assignment is a canonical project-pipeline write.
                project = Project(name="Fence assign flow")
                db.session.add(project)
                db.session.flush()
                flow = InterviewFlow(project_id=project.id, title="Flow")
                db.session.add(flow)
                db.session.flush()
                interview = Interview(project_id=project.id, status="transcribed", flow_id=None)
                db.session.add(interview)
                db.session.commit()
                interview_id = interview.id
                job = running_job(project.id, 5101)

                progress_calls = {"count": 0}

                def lose_after_progress(_job, _stage, **_details):
                    progress_calls["count"] += 1
                    supersede(job, 5201)

                raised = False
                try:
                    pipeline_service.run_project_pipeline(job, lose_after_progress)
                except JobLeaseLost:
                    raised = True
                db.session.expire_all()
                failures += check(
                    "stale pipeline cannot assign interview flow",
                    raised
                    and progress_calls["count"] == 1
                    and db.session.get(Interview, interview_id).flow_id is None,
                    f"raised={raised} flow_id={db.session.get(Interview, interview_id).flow_id}",
                )

                # 2) Existing completed transcription must not let a stale pipeline
                # advance interview.status after the precheck lease becomes stale.
                project2 = Project(name="Fence existing transcription")
                db.session.add(project2)
                db.session.flush()
                interview2 = Interview(project_id=project2.id, status="pending")
                db.session.add(interview2)
                db.session.flush()
                media2 = MediaFile(
                    interview_id=interview2.id,
                    original_filename="done.wav",
                    stored_path="done.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(media2)
                db.session.flush()
                done_tr = Transcription(
                    media_file_id=media2.id,
                    whisper_model="fake",
                    language="ja",
                    status="done",
                )
                db.session.add(done_tr)
                db.session.commit()
                interview2_id = interview2.id
                job2 = running_job(project2.id, 5102)

                saved_assert = pipeline_service.assert_job_lease
                assert_calls = {"count": 0}

                def lose_after_assert(target_job):
                    current = saved_assert(target_job)
                    assert_calls["count"] += 1
                    if assert_calls["count"] == 1:
                        supersede(job2, 5202)
                    return current

                pipeline_service.assert_job_lease = lose_after_assert
                raised = False
                try:
                    pipeline_service.run_project_pipeline(job2, lambda *_args, **_kwargs: None)
                except JobLeaseLost:
                    raised = True
                finally:
                    pipeline_service.assert_job_lease = saved_assert
                db.session.expire_all()
                failures += check(
                    "stale pipeline cannot advance status from existing transcription",
                    raised
                    and db.session.get(Interview, interview2_id).status == "pending",
                    f"raised={raised} status={db.session.get(Interview, interview2_id).status}",
                )

                # 3) Speaker-role auto assignment commits internally. The pipeline
                # write guard must reject a stale attempt before any role changes.
                project3 = Project(name="Fence auto roles")
                db.session.add(project3)
                db.session.flush()
                interview3 = Interview(project_id=project3.id, status="transcribed")
                db.session.add(interview3)
                db.session.flush()
                segment3 = Segment(
                    interview_id=interview3.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="unknown",
                    text="answer",
                    seq=0,
                )
                db.session.add(segment3)
                db.session.commit()
                interview3_id = interview3.id
                segment3_id = segment3.id
                job3 = running_job(project3.id, 5103)

                raised = False

                def stale_before_roles(_job, _stage, **_details):
                    supersede(job3, 5203)

                try:
                    pipeline_service.run_project_pipeline(job3, stale_before_roles)
                except JobLeaseLost:
                    raised = True
                db.session.expire_all()
                failures += check(
                    "stale pipeline cannot auto-assign speaker roles",
                    raised
                    and db.session.get(Segment, segment3_id).speaker_role == "unknown"
                    and db.session.get(Interview, interview3_id).status == "transcribed",
                    (
                        f"raised={raised} role={db.session.get(Segment, segment3_id).speaker_role} "
                        f"status={db.session.get(Interview, interview3_id).status}"
                    ),
                )

                # 4) Cleanup is allowed while the lease is current, but if the lease
                # changes after that commit the old attempt must not create a fresh
                # Transcription row for itself.
                project4 = Project(name="Fence transcription creation")
                db.session.add(project4)
                db.session.flush()
                interview4 = Interview(project_id=project4.id, status="pending")
                db.session.add(interview4)
                db.session.flush()
                media4 = MediaFile(
                    interview_id=interview4.id,
                    original_filename="retry.wav",
                    stored_path="retry.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(media4)
                db.session.flush()
                partial_tr = Transcription(
                    media_file_id=media4.id,
                    whisper_model="fake",
                    language="ja",
                    status="error",
                    error_message="partial",
                )
                db.session.add(partial_tr)
                db.session.flush()
                partial_segment = Segment(
                    transcription_id=partial_tr.id,
                    interview_id=interview4.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    text="partial result",
                    seq=0,
                )
                db.session.add(partial_segment)
                db.session.commit()
                media4_id = media4.id
                interview4_id = interview4.id
                partial_tr_id = partial_tr.id
                job4 = running_job(project4.id, 5104)

                saved_cleanup = pipeline_service.discard_incomplete_transcription_segments

                def cleanup_then_lose(media_file_id):
                    result = saved_cleanup(media_file_id)
                    supersede(job4, 5204)
                    return result

                pipeline_service.discard_incomplete_transcription_segments = cleanup_then_lose
                raised = False
                try:
                    pipeline_service.run_project_pipeline(job4, lambda *_args, **_kwargs: None)
                except JobLeaseLost:
                    raised = True
                finally:
                    pipeline_service.discard_incomplete_transcription_segments = saved_cleanup
                db.session.expire_all()
                transcriptions = Transcription.query.filter_by(media_file_id=media4_id).all()
                remaining_segments = Segment.query.filter_by(interview_id=interview4_id).all()
                failures += check(
                    "lease loss after cleanup blocks stale transcription creation",
                    raised
                    and len(transcriptions) == 1
                    and transcriptions[0].id == partial_tr_id
                    and transcriptions[0].status == "error"
                    and len(remaining_segments) == 0,
                    (
                        f"raised={raised} trs={[(t.id, t.status) for t in transcriptions]} "
                        f"segments={len(remaining_segments)}"
                    ),
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "project pipeline write fencing smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
