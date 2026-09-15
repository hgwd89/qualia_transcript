from __future__ import annotations

import hashlib
import os
import re
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


def run_isolated_mapping_smoke(
    *,
    source_db: Path,
    provider_call: Callable[..., dict] | None = None,
) -> int:
    """Run one mapping provider call using only a disposable research database."""
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    source_db = source_db.expanduser().resolve()
    source_before = _database_recovery_snapshot(source_db)
    status_rc_before, status_before, status_err_before = git_status(repo_root)
    failures += 0 if print_result(
        "git status readable before mapping provider smoke",
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
    }

    for key in (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    ):
        os.environ.pop(key, None)

    app = None
    release_runtime_locks = None
    try:
        with tempfile.TemporaryDirectory(prefix="qualia_mapping_provider_smoke_") as tmp:
            root = Path(tmp)
            temp_db = root / "mapping-smoke.db"
            config.DATABASE_URI = f"sqlite:///{temp_db.as_posix()}"
            config.UPLOAD_DIR = str(root / "uploads")
            config.OUTPUT_DIR = str(root / "outputs")
            config.BACKUP_DIR = str(root / "backups")
            config.RUNTIME_LOCK_PATH = str(root / "runtime.lock")

            from app import create_app
            from models import db
            from models.interview import Interview
            from models.interview_flow import (
                InterviewFlow,
                InterviewFlowQuestion,
                InterviewFlowSection,
            )
            from models.project import Project
            from models.segment import Segment, UtteranceMapping
            from models.setting import AppSetting
            from services.mapping_source_provenance import validate_current_ai_mapping_batch
            from services.runtime_lock import release_process_runtime_locks
            import services.mapper as mapper

            release_runtime_locks = release_process_runtime_locks
            try:
                app = create_app()
                app.config["TESTING"] = True
                with app.app_context():
                    if stored_openai_secret and not config.OPENAI_API_KEY:
                        AppSetting.set("openai_api_key", stored_openai_secret)

                    project = Project(name="Mapping provider smoke fixture", client="Test")
                    db.session.add(project)
                    db.session.flush()
                    flow = InterviewFlow(project_id=project.id, title="Provider smoke guide")
                    db.session.add(flow)
                    db.session.flush()
                    section = InterviewFlowSection(
                        flow_id=flow.id,
                        title="Background",
                        description="",
                        seq=1,
                    )
                    db.session.add(section)
                    db.session.flush()
                    q1 = InterviewFlowQuestion(
                        section_id=section.id,
                        question_code="Q1",
                        question_text="どこから来ましたか？",
                        question_type="open",
                        seq=1,
                    )
                    q2 = InterviewFlowQuestion(
                        section_id=section.id,
                        question_code="Q2",
                        question_text="いつ上京しましたか？",
                        question_type="open",
                        seq=2,
                    )
                    db.session.add_all([q1, q2])
                    db.session.flush()
                    interview = Interview(
                        project_id=project.id,
                        flow_id=flow.id,
                        status="transcribed",
                    )
                    db.session.add(interview)
                    db.session.flush()
                    segment = Segment(
                        interview_id=interview.id,
                        speaker_label="SPEAKER_01",
                        speaker_role="respondent",
                        start_sec=0.0,
                        end_sec=2.0,
                        text="大学の時に上京しました。",
                        seq=1,
                    )
                    db.session.add(segment)
                    db.session.commit()

                    interview_id = int(interview.id)
                    segment_id = int(segment.id)
                    q2_id = int(q2.id)
                    failures += 0 if print_result(
                        "SQLAlchemy is bound to disposable mapping DB",
                        temp_db.as_posix() in str(db.engine.url),
                        f"engine={db.engine.url}",
                    ) else 1

                    counter = {"n": 0}
                    original_call = mapper.call_structured
                    chosen_call = provider_call or original_call

                    def counted_call(*args, **kwargs):
                        counter["n"] += 1
                        return chosen_call(*args, **kwargs)

                    mapper.call_structured = counted_call
                    run_ok = True
                    run_error = ""
                    mapped_count = None
                    try:
                        mapped_count = mapper.run_mapping(
                            interview_id,
                            result_write_guard=lambda: None,
                        )
                    except Exception as exc:
                        run_ok = False
                        run_error = f"{type(exc).__name__}: {exc}"
                    finally:
                        mapper.call_structured = original_call

                    failures += 0 if print_result(
                        "isolated run_mapping executed",
                        run_ok,
                        run_error,
                    ) else 1
                    failures += 0 if print_result(
                        "mapping provider call count == 1",
                        counter["n"] == 1,
                        f"count={counter['n']}",
                    ) else 1
                    failures += 0 if print_result(
                        "exactly one disposable respondent segment mapped",
                        mapped_count == 1,
                        f"mapped_count={mapped_count}",
                    ) else 1

                    row = (
                        UtteranceMapping.query
                        .filter_by(segment_id=segment_id)
                        .order_by(UtteranceMapping.id.desc())
                        .first()
                    )
                    failures += 0 if print_result(
                        "disposable mapping exists",
                        row is not None,
                    ) else 1
                    if row is not None:
                        failures += 0 if print_result(
                            "provider maps fixture answer to Q2",
                            row.question_id == q2_id and row.is_unclassified is False,
                            f"question_id={row.question_id}, expected={q2_id}, unclassified={row.is_unclassified}",
                        ) else 1

                    current, reason, proof_count = validate_current_ai_mapping_batch(interview_id)
                    failures += 0 if print_result(
                        "disposable mapping generation has current provenance",
                        current and proof_count == 1,
                        f"current={current}, count={proof_count}, reason={reason}",
                    ) else 1
                    refreshed = db.session.get(Interview, interview_id)
                    failures += 0 if print_result(
                        "only disposable interview becomes mapped",
                        refreshed is not None and refreshed.status == "mapped",
                        f"status={refreshed.status if refreshed else None}",
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

    source_after = _database_recovery_snapshot(source_db)
    failures += 0 if print_result(
        "canonical SQLite durable bytes unchanged by mapping smoke",
        source_after == source_before,
        f"before={source_before!r} after={source_after!r}",
    ) else 1
    status_rc_after, status_after, status_err_after = git_status(repo_root)
    failures += 0 if print_result(
        "git status readable after mapping provider smoke",
        status_rc_after == 0,
        status_err_after.strip(),
    ) else 1
    failures += 0 if print_result(
        "repository working-tree state unchanged by mapping smoke",
        status_after == status_before,
        f"before={status_before!r} after={status_after!r}",
    ) else 1

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    mode = "injected provider" if provider_call is not None else "OpenAI provider"
    print(f"\nSummary: PASS ({mode}, disposable mapping fixture only)")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import config
    return run_isolated_mapping_smoke(
        source_db=Path(config.DATABASE_PATH),
        provider_call=None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
