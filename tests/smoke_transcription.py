from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return ok


def git_status(repo_root: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_recovery_snapshot(source_db: Path) -> dict[str, tuple[int, str] | None]:
    snapshot: dict[str, tuple[int, str] | None] = {}
    for suffix in ("", "-wal"):
        path = Path(f"{source_db}{suffix}")
        key = suffix or "db"
        if path.is_file():
            snapshot[key] = (path.stat().st_size, _sha256_file(path))
        else:
            snapshot[key] = None
    return snapshot


def _read_stored_openai_secret(source_db: Path) -> str | None:
    if not source_db.is_file():
        return None
    con = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only=ON")
        try:
            row = con.execute(
                "SELECT value FROM app_settings WHERE key='openai_api_key'"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        return str(row[0]) if row and row[0] else None
    finally:
        con.close()


def run_isolated_transcription_smoke(
    *,
    source_db: Path,
    source_audio: Path,
    client_factory: Callable[[], object] | None = None,
) -> int:
    """Run one transcription provider call using only disposable canonical state."""
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    source_db = source_db.expanduser().resolve()
    source_audio = source_audio.expanduser().resolve()
    source_before = _database_recovery_snapshot(source_db)
    source_audio_before = _sha256_file(source_audio) if source_audio.is_file() else None
    failures += 0 if print_result(
        "provider smoke source audio exists",
        source_audio.is_file(),
        f"path={source_audio}",
    ) else 1
    if not source_audio.is_file():
        return 1

    status_rc_before, status_before, status_err_before = git_status(repo_root)
    failures += 0 if print_result(
        "git status readable before transcription provider smoke",
        status_rc_before == 0,
        status_err_before.strip(),
    ) else 1

    import config

    stored_openai_secret = _read_stored_openai_secret(source_db)
    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
        "RUNTIME_LOCK_PATH": config.RUNTIME_LOCK_PATH,
        "TRANSCRIPTION_PROVIDER": config.TRANSCRIPTION_PROVIDER,
        "TRANSCRIPTION_FALLBACK_PROVIDER": config.TRANSCRIPTION_FALLBACK_PROVIDER,
    }

    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        os.environ.pop(key, None)

    app = None
    release_runtime_locks = None
    try:
        with tempfile.TemporaryDirectory(prefix="qualia_transcription_provider_smoke_") as tmp:
            root = Path(tmp)
            temp_db = root / "transcription-smoke.db"
            config.DATABASE_URI = f"sqlite:///{temp_db.as_posix()}"
            config.UPLOAD_DIR = str(root / "uploads")
            config.OUTPUT_DIR = str(root / "outputs")
            config.BACKUP_DIR = str(root / "backups")
            config.RUNTIME_LOCK_PATH = str(root / "runtime.lock")
            config.TRANSCRIPTION_PROVIDER = "openai"
            config.TRANSCRIPTION_FALLBACK_PROVIDER = ""

            from app import create_app
            from models import db
            from models.interview import Interview, Transcription
            from models.project import Project
            from models.segment import Segment
            from models.setting import AppSetting
            from services.runtime_lock import release_process_runtime_locks
            from services.upload_manager import save_and_register_media
            from werkzeug.datastructures import FileStorage
            import services.transcription as transcription_service

            release_runtime_locks = release_process_runtime_locks
            try:
                app = create_app()
                app.config["TESTING"] = True
                with app.app_context():
                    if stored_openai_secret and not config.OPENAI_API_KEY:
                        AppSetting.set("openai_api_key", stored_openai_secret)

                    project = Project(name="Transcription provider smoke fixture", client="Test")
                    db.session.add(project)
                    db.session.flush()
                    interview = Interview(
                        project_id=project.id,
                        participant_id=None,
                        flow_id=None,
                        interviewer_name="provider_smoke",
                        location="disposable_fixture",
                        status="pending",
                    )
                    db.session.add(interview)
                    db.session.commit()

                    with source_audio.open("rb") as source_stream:
                        media = save_and_register_media(
                            FileStorage(
                                stream=source_stream,
                                filename=source_audio.name,
                                content_type="audio/wav",
                            ),
                            interview,
                            original_filename=source_audio.name,
                            mime_type="audio/wav",
                        )
                    media.duration_sec = 30.0
                    db.session.commit()

                    model_name = transcription_service.get_default_transcription_model("openai")
                    transcription = Transcription(
                        media_file_id=media.id,
                        whisper_model=model_name,
                        language="ja",
                        status="pending",
                    )
                    db.session.add(transcription)
                    db.session.commit()
                    transcription_id = int(transcription.id)
                    interview_id = int(interview.id)

                    failures += 0 if print_result(
                        "SQLAlchemy is bound to disposable transcription DB",
                        temp_db.as_posix() in str(db.engine.url),
                        f"engine={db.engine.url}",
                    ) else 1
                    failures += 0 if print_result(
                        "managed provider audio was copied into disposable upload root",
                        Path(config.UPLOAD_DIR).resolve() in Path(config.UPLOAD_DIR, media.stored_path).resolve().parents
                        and _sha256_file(Path(config.UPLOAD_DIR, media.stored_path)) == source_audio_before,
                        f"stored_path={media.stored_path}",
                    ) else 1

                    api_call_count = {"n": 0}
                    original_factory = transcription_service._openai_client
                    chosen_factory = client_factory or original_factory

                    def wrapped_factory():
                        client = chosen_factory()
                        original_create = client.audio.transcriptions.create

                        def wrapped_create(*args, **kwargs):
                            api_call_count["n"] += 1
                            return original_create(*args, **kwargs)

                        client.audio.transcriptions.create = wrapped_create
                        return client

                    transcription_service._openai_client = wrapped_factory
                    run_ok = True
                    run_error = ""
                    result_payload = None
                    try:
                        result_payload = transcription_service.run_transcription(
                            transcription_id,
                            lease_check=lambda: None,
                            result_write_guard=lambda: None,
                        )
                    except Exception as exc:
                        run_ok = False
                        run_error = f"{type(exc).__name__}: {exc}"
                    finally:
                        transcription_service._openai_client = original_factory

                    failures += 0 if print_result(
                        "isolated run_transcription executed",
                        run_ok,
                        run_error,
                    ) else 1
                    failures += 0 if print_result(
                        "transcription provider call count == 1",
                        api_call_count["n"] == 1,
                        f"count={api_call_count['n']}",
                    ) else 1

                    latest = db.session.get(Transcription, transcription_id)
                    failures += 0 if print_result(
                        "disposable Transcription.status=done",
                        latest is not None and latest.status == "done",
                        f"status={latest.status if latest else None}",
                    ) else 1
                    segments = (
                        Segment.query
                        .filter_by(transcription_id=transcription_id)
                        .order_by(Segment.seq.asc(), Segment.id.asc())
                        .all()
                    )
                    failures += 0 if print_result(
                        "disposable transcription produced non-empty segments",
                        bool(segments) and any((row.text or "").strip() for row in segments),
                        f"count={len(segments)}",
                    ) else 1
                    refreshed_interview = db.session.get(Interview, interview_id)
                    failures += 0 if print_result(
                        "only disposable interview becomes transcribed",
                        refreshed_interview is not None and refreshed_interview.status == "transcribed",
                        f"status={refreshed_interview.status if refreshed_interview else None}",
                    ) else 1

                    raw_snapshot_path = ""
                    if isinstance(result_payload, dict):
                        raw_snapshot_path = str(result_payload.get("raw_snapshot_path") or "")
                    failures += 0 if print_result(
                        "immutable raw evidence is written only under disposable output root",
                        bool(raw_snapshot_path)
                        and Path(config.OUTPUT_DIR, raw_snapshot_path).is_file(),
                        f"raw_snapshot_path={raw_snapshot_path}",
                    ) else 1

                    db.session.remove()
                    db.engine.dispose()
            finally:
                if app is not None:
                    try:
                        with app.app_context():
                            db.session.remove()
                            db.engine.dispose()
                    except Exception:
                        pass
                if release_runtime_locks is not None:
                    release_runtime_locks()
    finally:
        config.DATABASE_URI = original_config["DATABASE_URI"]
        config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
        config.OUTPUT_DIR = original_config["OUTPUT_DIR"]
        config.BACKUP_DIR = original_config["BACKUP_DIR"]
        config.RUNTIME_LOCK_PATH = original_config["RUNTIME_LOCK_PATH"]
        config.TRANSCRIPTION_PROVIDER = original_config["TRANSCRIPTION_PROVIDER"]
        config.TRANSCRIPTION_FALLBACK_PROVIDER = original_config["TRANSCRIPTION_FALLBACK_PROVIDER"]

    source_after = _database_recovery_snapshot(source_db)
    source_audio_after = _sha256_file(source_audio)
    failures += 0 if print_result(
        "canonical SQLite durable bytes unchanged by transcription smoke",
        source_after == source_before,
        f"before={source_before!r} after={source_after!r}",
    ) else 1
    failures += 0 if print_result(
        "source provider audio bytes unchanged",
        source_audio_after == source_audio_before,
        f"before={source_audio_before} after={source_audio_after}",
    ) else 1

    status_rc_after, status_after, status_err_after = git_status(repo_root)
    failures += 0 if print_result(
        "git status readable after transcription provider smoke",
        status_rc_after == 0,
        status_err_after.strip(),
    ) else 1
    failures += 0 if print_result(
        "repository working-tree state unchanged by transcription smoke",
        status_after == status_before,
        f"before={status_before!r} after={status_after!r}",
    ) else 1

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    mode = "injected provider" if client_factory is not None else "OpenAI provider"
    print(f"\nSummary: PASS ({mode}, disposable transcription fixture only)")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import config

    return run_isolated_transcription_smoke(
        source_db=Path(config.DATABASE_PATH),
        source_audio=Path(config.UPLOAD_DIR) / "test_30s.wav",
        client_factory=None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
