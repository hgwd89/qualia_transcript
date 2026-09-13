from __future__ import annotations

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

    with tempfile.TemporaryDirectory(prefix="qualia_transcription_state_fence_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'state-fence.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")

        try:
            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile, Transcription
            from models.processing_job import ProcessingJob
            from models.project import Project
            import services.processing_jobs as processing_jobs
            import services.processing_result_guard as result_guard
            import services.transcription as transcription
            import services.transcription_dispatch as dispatch

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Transcription state fence smoke")
                db.session.add(project)
                db.session.flush()
                interview = Interview(project_id=project.id, status="pending")
                db.session.add(interview)
                db.session.flush()
                media = MediaFile(
                    interview_id=interview.id,
                    original_filename="fixture.wav",
                    stored_path=f"{interview.id}/fixture.wav",
                    file_type="audio",
                    mime_type="audio/wav",
                    duration_sec=1.0,
                    file_size_bytes=1,
                )
                job = ProcessingJob(
                    project_id=project.id,
                    interview_id=interview.id,
                    job_type="transcribe",
                    status="running",
                    attempt_count=1,
                )
                db.session.add_all([media, job])
                db.session.commit()
                job._lease_attempt = 1

                originals = {
                    "update_progress": processing_jobs.update_progress,
                    "begin_job_result_write": processing_jobs.begin_job_result_write,
                    "discard_incomplete": result_guard.discard_incomplete_transcription_segments,
                    "dispatch_run": dispatch.run_transcription,
                    "create_media_read_snapshot": transcription.create_media_read_snapshot,
                    "openai_client": transcription._openai_client,
                    "call_openai": transcription._call_openai_transcription,
                    "write_raw": transcription._write_raw_transcript_snapshot,
                }

                try:
                    events: list[str] = []

                    def fake_progress(_job, stage, **details):
                        suffix = "-id" if details.get("transcription_id") is not None else ""
                        events.append(f"progress:{stage}{suffix}")

                    def fake_guard(_job):
                        events.append("guard")
                        return _job

                    def fake_cleanup(media_file_id):
                        events.append("cleanup")
                        assert int(media_file_id) == int(media.id)
                        return {"transcription_ids": [], "deleted_segment_count": 0}

                    def fake_dispatch(transcription_id, *, lease_check=None, result_write_guard=None):
                        events.append("provider")
                        assert int(transcription_id) > 0
                        assert callable(lease_check)
                        assert callable(result_write_guard)
                        return {"segment_count": 0, "word_count": 0}

                    processing_jobs.update_progress = fake_progress
                    processing_jobs.begin_job_result_write = fake_guard
                    result_guard.discard_incomplete_transcription_segments = fake_cleanup
                    dispatch.run_transcription = fake_dispatch

                    preflight = processing_jobs._perform_transcription(job)
                    preflight_tr_id = int(preflight["transcription_id"])
                    failures += check(
                        "standalone transcribe job fences cleanup and pending-attempt creation separately",
                        events == [
                            "progress:transcribing",
                            "guard",
                            "cleanup",
                            "guard",
                            "progress:transcribing-id",
                            "provider",
                        ],
                        f"events={events!r} result={preflight!r}",
                    )

                    before_count = Transcription.query.count()
                    cleanup_started = False
                    events.clear()

                    def lost_preflight_guard(_job):
                        events.append("guard-lost")
                        raise RuntimeError("simulated preflight result-write lease loss")

                    def cleanup_must_not_run(_media_file_id):
                        nonlocal cleanup_started
                        cleanup_started = True
                        raise AssertionError("cleanup ran after preflight lease loss")

                    processing_jobs.begin_job_result_write = lost_preflight_guard
                    result_guard.discard_incomplete_transcription_segments = cleanup_must_not_run
                    preflight_lost = False
                    try:
                        processing_jobs._perform_transcription(job)
                    except RuntimeError as exc:
                        preflight_lost = "preflight result-write lease loss" in str(exc)
                    after_count = Transcription.query.count()
                    failures += check(
                        "preflight lease loss blocks stale cleanup and pending transcription creation",
                        preflight_lost
                        and not cleanup_started
                        and after_count == before_count,
                        (
                            f"lost={preflight_lost} cleanup_started={cleanup_started} "
                            f"before={before_count} after={after_count} events={events!r}"
                        ),
                    )

                    processing_jobs.begin_job_result_write = originals["begin_job_result_write"]
                    result_guard.discard_incomplete_transcription_segments = originals["discard_incomplete"]
                    processing_jobs.update_progress = originals["update_progress"]
                    dispatch.run_transcription = originals["dispatch_run"]

                    tr = db.session.get(Transcription, preflight_tr_id)
                    tr.status = "pending"
                    tr.whisper_model = "whisper-1"
                    tr.error_message = None
                    db.session.commit()

                    class FakeSnapshot:
                        full_path = str(root / "unused.wav")

                        def close(self):
                            events.append("snapshot-close")

                    guard_calls = 0
                    events.clear()

                    def success_guard():
                        nonlocal guard_calls
                        guard_calls += 1
                        events.append(f"guard-{guard_calls}")
                        return object()

                    def fake_snapshot(_media):
                        events.append("snapshot-open")
                        return FakeSnapshot()

                    def fake_call(_client, _path, _model, _language):
                        events.append("provider-call")
                        return {"text": "回答です。"}

                    def fake_raw(**_kwargs):
                        events.append("raw-write")
                        return "raw_transcripts/fake.json", "fake-digest"

                    transcription.create_media_read_snapshot = fake_snapshot
                    transcription._openai_client = lambda: object()
                    transcription._call_openai_transcription = fake_call
                    transcription._write_raw_transcript_snapshot = fake_raw

                    openai_result = transcription.run_openai_transcription(
                        preflight_tr_id,
                        result_write_guard=success_guard,
                    )
                    refreshed = db.session.get(Transcription, preflight_tr_id)
                    failures += check(
                        "OpenAI non-chunk path reserves running state and evidence/canonical result writes",
                        guard_calls == 2
                        and events.index("guard-1") < events.index("snapshot-open")
                        and events.index("provider-call") < events.index("guard-2")
                        and events.index("guard-2") < events.index("raw-write")
                        and refreshed.status == "done"
                        and openai_result.get("raw_snapshot_path") == "raw_transcripts/fake.json",
                        f"events={events!r} status={refreshed.status} result={openai_result!r}",
                    )

                    error_tr = Transcription(
                        media_file_id=media.id,
                        whisper_model="whisper-1",
                        language="ja",
                        status="pending",
                    )
                    db.session.add(error_tr)
                    db.session.commit()
                    error_tr_id = int(error_tr.id)

                    def snapshot_failure(_media):
                        raise RuntimeError("simulated snapshot failure")

                    transcription.create_media_read_snapshot = snapshot_failure
                    guard_calls = 0

                    def guard_then_lose():
                        nonlocal guard_calls
                        guard_calls += 1
                        if guard_calls == 2:
                            raise RuntimeError("simulated error-transition lease loss")
                        return object()

                    openai_lost = False
                    try:
                        transcription.run_openai_transcription(
                            error_tr_id,
                            result_write_guard=guard_then_lose,
                        )
                    except RuntimeError as exc:
                        openai_lost = "error-transition lease loss" in str(exc)
                    db.session.expire_all()
                    openai_after = db.session.get(Transcription, error_tr_id)
                    failures += check(
                        "OpenAI error transition is fenced and stale worker cannot overwrite running state",
                        openai_lost
                        and guard_calls == 2
                        and openai_after.status == "running"
                        and not openai_after.error_message,
                        (
                            f"lost={openai_lost} guards={guard_calls} "
                            f"status={openai_after.status} error={openai_after.error_message!r}"
                        ),
                    )

                    local_tr = Transcription(
                        media_file_id=media.id,
                        whisper_model="tiny",
                        language="ja",
                        status="pending",
                    )
                    db.session.add(local_tr)
                    db.session.commit()
                    local_tr_id = int(local_tr.id)
                    guard_calls = 0
                    local_lost = False
                    try:
                        transcription.run_local_whisper_transcription(
                            local_tr_id,
                            result_write_guard=guard_then_lose,
                        )
                    except RuntimeError as exc:
                        local_lost = "error-transition lease loss" in str(exc)
                    db.session.expire_all()
                    local_after = db.session.get(Transcription, local_tr_id)
                    failures += check(
                        "local Whisper running/error transitions use the same result-write fence",
                        local_lost
                        and guard_calls == 2
                        and local_after.status == "running"
                        and not local_after.error_message,
                        (
                            f"lost={local_lost} guards={guard_calls} "
                            f"status={local_after.status} error={local_after.error_message!r}"
                        ),
                    )

                finally:
                    processing_jobs.update_progress = originals["update_progress"]
                    processing_jobs.begin_job_result_write = originals["begin_job_result_write"]
                    result_guard.discard_incomplete_transcription_segments = originals["discard_incomplete"]
                    dispatch.run_transcription = originals["dispatch_run"]
                    transcription.create_media_read_snapshot = originals["create_media_read_snapshot"]
                    transcription._openai_client = originals["openai_client"]
                    transcription._call_openai_transcription = originals["call_openai"]
                    transcription._write_raw_transcript_snapshot = originals["write_raw"]

                db.session.remove()
                db.engine.dispose()

        except Exception as exc:
            failures += check("transcription state fencing smoke", False, f"{type(exc).__name__}: {exc}")
        finally:
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]

    transcription_source = (repo_root / "services" / "transcription.py").read_text(encoding="utf-8")
    processing_source = (repo_root / "services" / "processing_jobs.py").read_text(encoding="utf-8")

    chunk_error_start = transcription_source.find("except Exception as chunk_error:")
    chunk_error_end = transcription_source.find("offset += step", chunk_error_start)
    chunk_error_block = transcription_source[chunk_error_start:chunk_error_end]
    final_manifest_marker = "# Reserve the durable attempt before publishing the final chunk manifest."
    final_manifest_start = transcription_source.find(final_manifest_marker)
    final_manifest_end = transcription_source.find("tr.status = \"done\"", final_manifest_start)
    final_manifest_block = transcription_source[final_manifest_start:final_manifest_end]

    failures += check(
        "chunk error manifest is fenced before immutable evidence publication",
        chunk_error_start >= 0
        and chunk_error_block.find("db.session.rollback()") >= 0
        and chunk_error_block.find("result_write_guard()") >= 0
        and chunk_error_block.find("db.session.rollback()")
        < chunk_error_block.find("result_write_guard()")
        < chunk_error_block.find("manifest_path = _write_chunk_manifest"),
    )
    failures += check(
        "final chunk manifest is published under the same reservation as done state",
        final_manifest_start >= 0
        and final_manifest_block.find("result_write_guard()") >= 0
        and final_manifest_block.find("result_write_guard()")
        < final_manifest_block.find("manifest_path = _write_chunk_manifest"),
    )
    failures += check(
        "standalone transcription preflight has two explicit result-write reservations",
        processing_source.count("begin_job_result_write(job)") >= 2
        and "Fence cleanup separately" in processing_source
        and "Fence creation of the next canonical transcription attempt" in processing_source,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
