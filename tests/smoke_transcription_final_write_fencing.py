import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


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

    with tempfile.TemporaryDirectory(prefix="qualia_transcription_final_fence_") as tmp:
        root = Path(tmp)
        config.DATABASE_URI = f"sqlite:///{(root / 'final_fence.db').as_posix()}"
        config.UPLOAD_DIR = str(root / "uploads")
        config.OUTPUT_DIR = str(root / "outputs")
        Path(config.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
        Path(config.OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

        try:
            from app import create_app
            from models import db
            from models.interview import Interview, MediaFile, Transcription
            from models.project import Project
            from models.segment import Segment
            from services.processing_jobs import JobLeaseLost
            import services.transcription as transcription_service
            import services.transcription_dispatch as dispatch_service

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                project = Project(name="Transcription final write fencing")
                db.session.add(project)
                db.session.commit()

                def make_transcription(name: str, duration: float, model: str = "fake"):
                    interview = Interview(project_id=project.id, status="pending")
                    db.session.add(interview)
                    db.session.flush()
                    media = MediaFile(
                        interview_id=interview.id,
                        original_filename=f"{name}.wav",
                        stored_path=f"{name}.wav",
                        file_type="audio",
                        mime_type="audio/wav",
                        duration_sec=duration,
                    )
                    db.session.add(media)
                    db.session.flush()
                    tr = Transcription(
                        media_file_id=media.id,
                        whisper_model=model,
                        language="ja",
                        status="pending",
                    )
                    db.session.add(tr)
                    db.session.commit()
                    return interview.id, tr.id

                saved = {
                    "openai_client": transcription_service._openai_client,
                    "call_openai": transcription_service._call_openai_transcription,
                    "export_chunk": transcription_service._export_audio_chunk_wav,
                    "get_model": transcription_service._get_model,
                    "provider": dispatch_service.get_transcription_provider,
                    "fallback": dispatch_service.get_fallback_provider,
                }

                transcription_service._openai_client = lambda: object()
                transcription_service._call_openai_transcription = (
                    lambda _client, _path, _model, _language: {"text": "最初の発言です。次の発言です。"}
                )
                dispatch_service.get_fallback_provider = lambda: ""

                try:
                    # 1) OpenAI non-chunk: lease loss at the final write guard must
                    # prevent Segment/done state from becoming canonical.
                    stale_iv_id, stale_tr_id = make_transcription("openai_stale", 30.0)
                    stale_state = {"lost": False, "guard_calls": 0}

                    def stale_lease_check():
                        if stale_state["lost"]:
                            raise JobLeaseLost("simulated final-write lease loss")

                    def stale_write_guard():
                        stale_state["guard_calls"] += 1
                        if Segment.query.filter_by(transcription_id=stale_tr_id).count() != 0:
                            raise AssertionError("canonical segments existed before final write guard")
                        stale_state["lost"] = True
                        raise JobLeaseLost("simulated final-write lease loss")

                    dispatch_service.get_transcription_provider = lambda: "openai"
                    stale_raised = False
                    try:
                        dispatch_service.run_transcription(
                            stale_tr_id,
                            lease_check=stale_lease_check,
                            result_write_guard=stale_write_guard,
                        )
                    except JobLeaseLost:
                        stale_raised = True

                    stale_tr = db.session.get(Transcription, stale_tr_id)
                    stale_iv = db.session.get(Interview, stale_iv_id)
                    stale_segments = Segment.query.filter_by(transcription_id=stale_tr_id).all()
                    failures += check(
                        "OpenAI stale final write cannot become canonical",
                        stale_raised
                        and stale_state["guard_calls"] == 1
                        and stale_tr.status == "error"
                        and stale_iv.status == "pending"
                        and len(stale_segments) == 0,
                        f"raised={stale_raised} guard={stale_state['guard_calls']} "
                        f"tr={stale_tr.status} iv={stale_iv.status} segments={len(stale_segments)}",
                    )

                    # 2) OpenAI non-chunk current attempt still commits normally,
                    # and the guard runs before any canonical Segment exists.
                    fresh_iv_id, fresh_tr_id = make_transcription("openai_fresh", 30.0)
                    fresh_state = {"guard_calls": 0, "segments_at_guard": None}

                    def fresh_guard():
                        fresh_state["guard_calls"] += 1
                        fresh_state["segments_at_guard"] = Segment.query.filter_by(
                            transcription_id=fresh_tr_id
                        ).count()

                    result = dispatch_service.run_transcription(
                        fresh_tr_id,
                        lease_check=lambda: None,
                        result_write_guard=fresh_guard,
                    )
                    fresh_tr = db.session.get(Transcription, fresh_tr_id)
                    fresh_iv = db.session.get(Interview, fresh_iv_id)
                    fresh_segments = Segment.query.filter_by(transcription_id=fresh_tr_id).all()
                    failures += check(
                        "OpenAI current attempt commits behind final write guard",
                        fresh_state["guard_calls"] == 1
                        and fresh_state["segments_at_guard"] == 0
                        and fresh_tr.status == "done"
                        and fresh_iv.status == "transcribed"
                        and len(fresh_segments) == result.get("segment_count", -1)
                        and len(fresh_segments) > 0,
                        f"guard={fresh_state} tr={fresh_tr.status} iv={fresh_iv.status} "
                        f"segments={len(fresh_segments)} result={result}",
                    )

                    # 3) local Whisper is lazy. Inference must be fully materialized
                    # before the write guard, while DB Segment rows are still absent.
                    local_iv_id, local_tr_id = make_transcription("local_fresh", 20.0, model="tiny")
                    local_state = {"decoded": False, "guard_calls": 0, "segments_at_guard": None}

                    class FakeLocalModel:
                        def transcribe(self, *_args, **_kwargs):
                            def generate():
                                local_state["decoded"] = True
                                yield SimpleNamespace(
                                    speaker=None,
                                    start=0.0,
                                    end=1.0,
                                    text=" local first ",
                                )
                                yield SimpleNamespace(
                                    speaker=None,
                                    start=1.0,
                                    end=2.0,
                                    text=" local second ",
                                )
                            return generate(), SimpleNamespace()

                    transcription_service._get_model = lambda _name: FakeLocalModel()
                    dispatch_service.get_transcription_provider = lambda: "local_whisper"

                    def local_guard():
                        local_state["guard_calls"] += 1
                        local_state["segments_at_guard"] = Segment.query.filter_by(
                            transcription_id=local_tr_id
                        ).count()
                        if not local_state["decoded"]:
                            raise AssertionError("local inference had not completed before write guard")

                    local_result = dispatch_service.run_transcription(
                        local_tr_id,
                        lease_check=lambda: None,
                        result_write_guard=local_guard,
                    )
                    local_tr = db.session.get(Transcription, local_tr_id)
                    local_iv = db.session.get(Interview, local_iv_id)
                    local_segments = Segment.query.filter_by(transcription_id=local_tr_id).all()
                    failures += check(
                        "local Whisper decodes before taking final write reservation",
                        local_state["decoded"]
                        and local_state["guard_calls"] == 1
                        and local_state["segments_at_guard"] == 0
                        and local_tr.status == "done"
                        and local_iv.status == "transcribed"
                        and len(local_segments) == 2
                        and local_result.get("segment_count") == 2,
                        f"state={local_state} tr={local_tr.status} iv={local_iv.status} "
                        f"segments={len(local_segments)} result={local_result}",
                    )

                    # 4) Long OpenAI audio can commit completed chunks incrementally,
                    # but loss at the final promotion must invalidate all stale chunks
                    # instead of leaving a stale done transcript behind.
                    long_iv_id, long_tr_id = make_transcription("openai_long_stale", 610.0)
                    long_state = {"lost": False, "guard_calls": 0, "lease_calls": 0}
                    transcription_service._export_audio_chunk_wav = (
                        lambda src_path, out_path, start_sec, duration_sec: float(duration_sec)
                    )
                    dispatch_service.get_transcription_provider = lambda: "openai"

                    def long_lease_check():
                        long_state["lease_calls"] += 1
                        if long_state["lost"]:
                            raise JobLeaseLost("simulated long final-write lease loss")

                    def long_write_guard():
                        long_state["guard_calls"] += 1
                        # At least one successful chunk must already be durable here.
                        if Segment.query.filter_by(transcription_id=long_tr_id).count() == 0:
                            raise AssertionError("long-audio partial chunks were not committed")
                        long_state["lost"] = True
                        raise JobLeaseLost("simulated long final-write lease loss")

                    long_raised = False
                    try:
                        dispatch_service.run_transcription(
                            long_tr_id,
                            lease_check=long_lease_check,
                            result_write_guard=long_write_guard,
                        )
                    except JobLeaseLost:
                        long_raised = True

                    long_tr = db.session.get(Transcription, long_tr_id)
                    long_iv = db.session.get(Interview, long_iv_id)
                    long_segments = Segment.query.filter_by(transcription_id=long_tr_id).all()
                    failures += check(
                        "long-audio stale final promotion invalidates committed chunks",
                        long_raised
                        and long_state["guard_calls"] == 1
                        and long_state["lease_calls"] >= 3
                        and long_tr.status == "error"
                        and long_iv.status == "pending"
                        and len(long_segments) == 0,
                        f"raised={long_raised} state={long_state} tr={long_tr.status} "
                        f"iv={long_iv.status} segments={len(long_segments)}",
                    )

                finally:
                    transcription_service._openai_client = saved["openai_client"]
                    transcription_service._call_openai_transcription = saved["call_openai"]
                    transcription_service._export_audio_chunk_wav = saved["export_chunk"]
                    transcription_service._get_model = saved["get_model"]
                    dispatch_service.get_transcription_provider = saved["provider"]
                    dispatch_service.get_fallback_provider = saved["fallback"]

                db.session.remove()
                db.engine.dispose()

        except Exception as exc:
            failures += check(
                "transcription final write fencing smoke",
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
