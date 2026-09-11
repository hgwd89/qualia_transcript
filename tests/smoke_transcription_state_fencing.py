import sys
import tempfile
import threading
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
    app = None

    with tempfile.TemporaryDirectory(prefix="qualia_transcription_state_fencing_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'transcription_state.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile, Transcription
            from models.processing_job import ProcessingJob
            from models.project import Project
            from models.segment import Segment
            from services.processing_jobs import JobLeaseLost
            import services.processing_jobs as processing_jobs_service
            import services.processing_result_guard as result_guard

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Transcription state fencing")
                db.session.add(project)
                db.session.commit()
                project_id = project.id

                # -----------------------------------------------------------------
                # 1) Individual transcribe retry: cleanup may commit successfully,
                # but a lease loss immediately afterward must prevent the stale
                # worker from creating a fresh Transcription row.
                # -----------------------------------------------------------------
                interview = Interview(project_id=project_id, status="pending")
                db.session.add(interview)
                db.session.flush()
                media = MediaFile(
                    interview_id=interview.id,
                    original_filename="retry.wav",
                    stored_path="retry.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(media)
                db.session.flush()
                old_tr = Transcription(
                    media_file_id=media.id,
                    whisper_model="fake",
                    language="ja",
                    status="running",
                )
                db.session.add(old_tr)
                db.session.flush()
                db.session.add(Segment(
                    transcription_id=old_tr.id,
                    interview_id=interview.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    text="partial result",
                    seq=0,
                ))
                job = ProcessingJob(
                    project_id=project_id,
                    interview_id=interview.id,
                    job_type="transcribe",
                    status="running",
                    attempt_count=1,
                    worker_pid=6101,
                )
                db.session.add(job)
                db.session.commit()
                job._lease_attempt = 1
                media_id = media.id
                interview_id = interview.id
                old_tr_id = old_tr.id
                job_id = job.id

                original_cleanup = result_guard.discard_incomplete_transcription_segments

                def cleanup_then_supersede(media_file_id):
                    cleanup = original_cleanup(media_file_id)
                    (
                        ProcessingJob.query
                        .filter_by(id=job_id)
                        .update(
                            {
                                ProcessingJob.attempt_count: 2,
                                ProcessingJob.status: "running",
                                ProcessingJob.worker_pid: 6201,
                            },
                            synchronize_session=False,
                        )
                    )
                    db.session.commit()
                    return cleanup

                result_guard.discard_incomplete_transcription_segments = cleanup_then_supersede
                raised = False
                try:
                    processing_jobs_service._perform_transcription(job)
                except JobLeaseLost:
                    raised = True
                finally:
                    result_guard.discard_incomplete_transcription_segments = original_cleanup

                db.session.expire_all()
                transcriptions = Transcription.query.filter_by(media_file_id=media_id).all()
                remaining_segments = Segment.query.filter_by(interview_id=interview_id).all()
                current_job = db.session.get(ProcessingJob, job_id)
                failures += check(
                    "lease loss after retry cleanup blocks stale transcription creation",
                    raised
                    and len(transcriptions) == 1
                    and transcriptions[0].id == old_tr_id
                    and transcriptions[0].status == "error"
                    and len(remaining_segments) == 0
                    and current_job.attempt_count == 2
                    and current_job.worker_pid == 6201,
                    (
                        f"raised={raised} trs={[(t.id, t.status) for t in transcriptions]} "
                        f"segments={len(remaining_segments)} job={current_job.to_dict()}"
                    ),
                )

                # -----------------------------------------------------------------
                # 2) Stale-attempt invalidation versus a newer transcription
                # completion. Pause invalidation after it sees no other done row.
                # A competing completion must block on the invalidation write
                # reservation; after invalidation commits, completion wins last and
                # leaves interview.status='transcribed'.
                # -----------------------------------------------------------------
                race_interview = Interview(project_id=project_id, status="transcribed")
                db.session.add(race_interview)
                db.session.flush()
                race_media = MediaFile(
                    interview_id=race_interview.id,
                    original_filename="race.wav",
                    stored_path="race.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                )
                db.session.add(race_media)
                db.session.flush()
                stale_tr = Transcription(
                    media_file_id=race_media.id,
                    whisper_model="fake",
                    language="ja",
                    status="done",
                    completed_at=datetime.now(timezone.utc),
                )
                newer_tr = Transcription(
                    media_file_id=race_media.id,
                    whisper_model="fake",
                    language="ja",
                    status="running",
                )
                db.session.add_all([stale_tr, newer_tr])
                db.session.flush()
                db.session.add(Segment(
                    transcription_id=stale_tr.id,
                    interview_id=race_interview.id,
                    speaker_label="SPEAKER_00",
                    speaker_role="respondent",
                    text="stale canonical-looking result",
                    seq=0,
                ))
                db.session.commit()
                race_interview_id = race_interview.id
                stale_tr_id = stale_tr.id
                newer_tr_id = newer_tr.id

                queried = threading.Event()
                release_invalidation = threading.Event()
                completion_started = threading.Event()
                completion_done = threading.Event()
                thread_errors = []
                original_find_done = result_guard._find_other_done_transcription

                def paused_find_done(media_file_id, transcription_id):
                    found = original_find_done(media_file_id, transcription_id)
                    queried.set()
                    if not release_invalidation.wait(timeout=10):
                        raise RuntimeError("timed out waiting to release invalidation")
                    return found

                result_guard._find_other_done_transcription = paused_find_done

                def invalidate_worker():
                    try:
                        with app.app_context():
                            result_guard.invalidate_transcription_attempt(stale_tr_id)
                    except Exception as exc:
                        thread_errors.append(("invalidate", type(exc).__name__, str(exc)))
                    finally:
                        try:
                            with app.app_context():
                                db.session.remove()
                        except Exception:
                            pass

                def complete_newer_worker():
                    try:
                        with app.app_context():
                            tr = db.session.get(Transcription, newer_tr_id)
                            iv = db.session.get(Interview, race_interview_id)
                            tr.status = "done"
                            tr.completed_at = datetime.now(timezone.utc)
                            iv.status = "transcribed"
                            completion_started.set()
                            db.session.commit()
                            completion_done.set()
                    except Exception as exc:
                        thread_errors.append(("complete", type(exc).__name__, str(exc)))
                    finally:
                        try:
                            with app.app_context():
                                db.session.remove()
                        except Exception:
                            pass

                invalidate_thread = threading.Thread(target=invalidate_worker, daemon=True)
                complete_thread = threading.Thread(target=complete_newer_worker, daemon=True)
                try:
                    invalidate_thread.start()
                    if not queried.wait(timeout=10):
                        raise RuntimeError("invalidation did not reach competing-done query")
                    complete_thread.start()
                    if not completion_started.wait(timeout=10):
                        raise RuntimeError("newer completion did not start")

                    # While invalidation is paused after the 'no other done' query,
                    # the competing completion must be unable to commit.
                    completion_blocked = not completion_done.wait(timeout=0.75)
                    release_invalidation.set()
                    invalidate_thread.join(timeout=10)
                    complete_thread.join(timeout=10)
                finally:
                    release_invalidation.set()
                    result_guard._find_other_done_transcription = original_find_done

                db.session.expire_all()
                final_stale = db.session.get(Transcription, stale_tr_id)
                final_newer = db.session.get(Transcription, newer_tr_id)
                final_interview = db.session.get(Interview, race_interview_id)
                stale_segments = Segment.query.filter_by(transcription_id=stale_tr_id).all()
                failures += check(
                    "stale invalidation serializes with newer completion",
                    completion_blocked
                    and not invalidate_thread.is_alive()
                    and not complete_thread.is_alive()
                    and not thread_errors
                    and final_stale.status == "error"
                    and len(stale_segments) == 0
                    and final_newer.status == "done"
                    and final_interview.status == "transcribed",
                    (
                        f"blocked={completion_blocked} errors={thread_errors} "
                        f"stale={final_stale.status} newer={final_newer.status} "
                        f"interview={final_interview.status} stale_segments={len(stale_segments)}"
                    ),
                )

                db.session.remove()
                db.engine.dispose()
        except Exception as exc:
            failures += check(
                "transcription state fencing smoke",
                False,
                f"{type(exc).__name__}: {exc}",
            )
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
