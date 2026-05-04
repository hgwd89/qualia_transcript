import os
import subprocess
import sys
from pathlib import Path


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def run_git_status(repo_root: Path, label: str) -> bool:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    print(f"--- git status --short ({label}) ---")
    print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.strip())
    print("--- end ---")
    return proc.returncode == 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    failures += 0 if print_result(
        "git status --short before", run_git_status(repo_root, "before")
    ) else 1

    # このプロセス内だけプロキシ環境変数を外す（値は表示しない）
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        os.environ.pop(key, None)

    try:
        import config
        from app import create_app
        from models import db
        from models.project import Project
        from models.interview import Interview, MediaFile, Transcription
        from models.segment import Segment
        import services.transcription as transcription_service
    except Exception as e:
        failures += 0 if print_result("imports", False, f"{type(e).__name__}: {e}") else 1
        print("\nSummary: FAIL")
        return 1

    app = create_app()

    with app.app_context():
        provider = transcription_service.get_transcription_provider()
        model_name = transcription_service.get_default_transcription_model(provider)
        failures += 0 if print_result(
            "transcription provider is openai",
            provider == "openai",
            f"provider={provider}",
        ) else 1
        print(f"[INFO] transcription_model={model_name}")

        target_audio = Path(config.UPLOAD_DIR) / "test_30s.wav"
        failures += 0 if print_result(
            "uploads/test_30s.wav exists",
            target_audio.exists(),
            f"path={target_audio}",
        ) else 1
        if not target_audio.exists():
            print("\nSummary: FAIL")
            return 1

        project = Project.query.get(2)
        failures += 0 if print_result("project_id=2 exists", project is not None) else 1
        if project is None:
            print("\nSummary: FAIL")
            return 1

        # 既存検証対象の固定IDが汚染されていないことを軽く確認
        iv4 = Interview.query.get(4)
        tr8 = Transcription.query.get(8)
        seg40 = Segment.query.get(40)
        failures += 0 if print_result("interview_id=4 exists", iv4 is not None) else 1
        failures += 0 if print_result("transcription_id=8 exists", tr8 is not None) else 1
        failures += 0 if print_result("segment_id=40 exists", seg40 is not None) else 1

        api_call_count = {"n": 0}
        original_client_factory = transcription_service._openai_client

        def wrapped_client_factory():
            client = original_client_factory()
            original_create = client.audio.transcriptions.create

            def wrapped_create(*args, **kwargs):
                api_call_count["n"] += 1
                return original_create(*args, **kwargs)

            client.audio.transcriptions.create = wrapped_create
            return client

        transcription_service._openai_client = wrapped_client_factory

        created_interview_id = None
        created_media_id = None
        created_transcription_id = None
        cleanup_done = False

        try:
            test_interview = Interview(
                project_id=2,
                participant_id=None,
                flow_id=None,
                interviewer_name="smoke_transcription",
                location="local_test",
                status="pending",
            )
            db.session.add(test_interview)
            db.session.commit()
            created_interview_id = test_interview.id

            test_media = MediaFile(
                interview_id=test_interview.id,
                original_filename="test_30s.wav",
                stored_path="test_30s.wav",
                file_type="audio",
                mime_type="audio/wav",
                duration_sec=30.0,
                file_size_bytes=target_audio.stat().st_size,
            )
            db.session.add(test_media)
            db.session.commit()
            created_media_id = test_media.id

            test_transcription = Transcription(
                media_file_id=test_media.id,
                whisper_model=model_name,
                language="ja",
                status="pending",
            )
            db.session.add(test_transcription)
            db.session.commit()
            created_transcription_id = test_transcription.id

            run_ok = True
            run_error = ""
            result_payload = None
            try:
                result_payload = transcription_service.run_transcription(test_transcription.id)
            except Exception as e:
                run_ok = False
                run_error = f"{type(e).__name__}: {e}"

            failures += 0 if print_result("run_transcription executed", run_ok, run_error) else 1
            failures += 0 if print_result(
                "OpenAI API call count == 1",
                api_call_count["n"] == 1,
                f"count={api_call_count['n']}",
            ) else 1
            if result_payload is not None:
                print(f"[INFO] run_transcription_result={result_payload}")

            latest_tr = Transcription.query.get(test_transcription.id)
            tr_done = latest_tr is not None and latest_tr.status == "done"
            failures += 0 if print_result(
                "Transcription.status=done",
                tr_done,
                f"status={latest_tr.status if latest_tr else None}",
            ) else 1

            segs = Segment.query.filter_by(transcription_id=test_transcription.id).order_by(Segment.id.asc()).all()
            failures += 0 if print_result(
                "Segment count >= 1",
                len(segs) >= 1,
                f"count={len(segs)}",
            ) else 1

            non_empty_text = any((s.text or "").strip() for s in segs)
            failures += 0 if print_result(
                "Transcription text is non-empty",
                non_empty_text,
            ) else 1

        finally:
            # クリーンアップ: 作成した test interview/media/transcription/segments を削除
            try:
                if created_interview_id is not None:
                    target_interview = Interview.query.get(created_interview_id)
                    if target_interview is not None:
                        db.session.delete(target_interview)
                        db.session.commit()
                cleanup_done = True
            except Exception as e:
                db.session.rollback()
                cleanup_done = False
                print(f"[WARN] cleanup_error={type(e).__name__}: {e}")
            finally:
                transcription_service._openai_client = original_client_factory

        failures += 0 if print_result("cleanup done", cleanup_done) else 1
        print(f"[INFO] created_interview_id={created_interview_id}")
        print(f"[INFO] created_media_id={created_media_id}")
        print(f"[INFO] created_transcription_id={created_transcription_id}")

        # 既存の固定IDが残っていることを再確認（不変性チェック）
        iv4_after = Interview.query.get(4)
        tr8_after = Transcription.query.get(8)
        seg40_after = Segment.query.get(40)
        failures += 0 if print_result("interview_id=4 unchanged", iv4_after is not None) else 1
        failures += 0 if print_result("transcription_id=8 unchanged", tr8_after is not None) else 1
        failures += 0 if print_result("segment_id=40 unchanged", seg40_after is not None) else 1

    failures += 0 if print_result(
        "git status --short after", run_git_status(repo_root, "after")
    ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
