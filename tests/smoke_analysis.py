from __future__ import annotations

import hashlib
import json
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
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
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


def contains_japanese(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text or ""))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_recovery_snapshot(source_db: Path) -> dict[str, tuple[int, str] | None]:
    """Fingerprint durable SQLite bytes that must not change during the smoke."""
    snapshot: dict[str, tuple[int, str] | None] = {}
    for suffix in ("", "-wal"):
        path = Path(f"{source_db}{suffix}")
        if path.is_file():
            snapshot[suffix or "db"] = (path.stat().st_size, _sha256_file(path))
        else:
            snapshot[suffix or "db"] = None
    return snapshot


def _read_stored_openai_secret(source_db: Path) -> str | None:
    """Read only the provider credential needed by the optional paid smoke.

    The canonical research database is never initialized through SQLAlchemy by
    this test. On Windows a stored value may be a current-user DPAPI envelope;
    copying that envelope into the disposable fixture remains decryptable by the
    same account without exposing plaintext here.
    """
    if not source_db.is_file():
        return None
    con = sqlite3.connect(
        f"file:{source_db.as_posix()}?mode=ro",
        uri=True,
    )
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


def run_isolated_provider_smoke(
    *,
    source_db: Path,
    provider_call: Callable[..., dict] | None = None,
) -> int:
    """Run the analysis service against a disposable DB only.

    ``provider_call`` is injected by the providerless regression. When omitted,
    the real OpenAI path is invoked exactly once, making this function suitable
    for the explicitly requested local paid/provider check as well.
    """
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    source_db = source_db.expanduser().resolve()
    source_before = _database_recovery_snapshot(source_db)
    status_rc_before, status_before, status_err_before = git_status(repo_root)
    failures += 0 if print_result(
        "git status readable before provider smoke",
        status_rc_before == 0,
        status_err_before.strip(),
    ) else 1

    try:
        import config
    except Exception as exc:
        print_result("config import", False, f"{type(exc).__name__}: {exc}")
        return 1

    stored_openai_secret = _read_stored_openai_secret(source_db)
    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
        "RUNTIME_LOCK_PATH": config.RUNTIME_LOCK_PATH,
    }

    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(key, None)

    app = None
    release_runtime_locks = None
    try:
        with tempfile.TemporaryDirectory(prefix="qualia_analysis_provider_smoke_") as tmp:
            root = Path(tmp)
            temp_db = root / "analysis-smoke.db"
            config.DATABASE_URI = f"sqlite:///{temp_db.as_posix()}"
            config.UPLOAD_DIR = str(root / "uploads")
            config.OUTPUT_DIR = str(root / "outputs")
            config.BACKUP_DIR = str(root / "backups")
            config.RUNTIME_LOCK_PATH = str(root / "runtime.lock")

            try:
                from app import create_app
                from models import db
                from models.analysis import AIAnalysis
                from models.interview import Interview
                from models.participant import Participant
                from models.project import Project
                from models.segment import Segment
                from models.setting import AppSetting
                from services.runtime_lock import release_process_runtime_locks
                import services.analyzer as analyzer
            except Exception as exc:
                failures += 0 if print_result(
                    "isolated provider smoke imports",
                    False,
                    f"{type(exc).__name__}: {exc}",
                ) else 1
                return 1

            release_runtime_locks = release_process_runtime_locks
            try:
                app = create_app()
                app.config["TESTING"] = True

                with app.app_context():
                    if stored_openai_secret and not config.OPENAI_API_KEY:
                        AppSetting.set("openai_api_key", stored_openai_secret)

                    project = Project(name="Provider smoke fixture", client="Test")
                    db.session.add(project)
                    db.session.flush()
                    participant = Participant(
                        project_id=project.id,
                        participant_code="P01",
                        display_name="Smoke Participant",
                    )
                    db.session.add(participant)
                    db.session.flush()
                    interview = Interview(
                        project_id=project.id,
                        participant_id=participant.id,
                        status="transcribed",
                    )
                    db.session.add(interview)
                    db.session.flush()
                    db.session.add(Segment(
                        interview_id=interview.id,
                        participant_id=participant.id,
                        speaker_label="SPEAKER_00",
                        speaker_role="respondent",
                        start_sec=0.0,
                        end_sec=1.0,
                        text="テスト用の回答です",
                        seq=1,
                    ))
                    db.session.commit()
                    interview_id = int(interview.id)

                    failures += 0 if print_result(
                        "SQLAlchemy is bound to disposable database",
                        temp_db.resolve() != source_db
                        and temp_db.as_posix() in str(db.engine.url),
                        f"engine={db.engine.url}",
                    ) else 1

                    count_before = AIAnalysis.query.count()
                    counter = {"n": 0}
                    original_call = analyzer.call_structured
                    chosen_call = provider_call or original_call

                    def counted_call(*args, **kwargs):
                        counter["n"] += 1
                        return chosen_call(*args, **kwargs)

                    analyzer.call_structured = counted_call
                    analyze_ok = True
                    error_detail = ""
                    saved_id = None
                    try:
                        analysis = analyzer.analyze_interview_summary(
                            interview_id,
                            result_write_guard=lambda: None,
                        )
                        saved_id = int(analysis.id)
                    except Exception as exc:
                        analyze_ok = False
                        error_detail = f"{type(exc).__name__}: {exc}"
                    finally:
                        analyzer.call_structured = original_call

                    failures += 0 if print_result(
                        "isolated analyze_interview_summary executed",
                        analyze_ok,
                        error_detail,
                    ) else 1
                    failures += 0 if print_result(
                        "provider call count == 1",
                        counter["n"] == 1,
                        f"count={counter['n']}",
                    ) else 1

                    count_after = AIAnalysis.query.count()
                    failures += 0 if print_result(
                        "disposable AIAnalysis count increased",
                        count_after == count_before + 1,
                        f"before={count_before}, after={count_after}",
                    ) else 1

                    interview_after = db.session.get(Interview, interview_id)
                    failures += 0 if print_result(
                        "only disposable interview becomes analyzed",
                        interview_after is not None and interview_after.status == "analyzed",
                        f"status={interview_after.status if interview_after else None}",
                    ) else 1

                    latest = db.session.get(AIAnalysis, saved_id) if saved_id else None
                    failures += 0 if print_result(
                        "disposable analysis exists",
                        latest is not None,
                    ) else 1

                    if latest is not None:
                        failures += 0 if print_result(
                            "analysis_type is per_participant",
                            latest.analysis_type == "per_participant",
                            f"type={latest.analysis_type}",
                        ) else 1
                        payload = {}
                        try:
                            payload = json.loads(latest.content_json or "{}")
                        except Exception as exc:
                            failures += 0 if print_result(
                                "content_json parse",
                                False,
                                f"{type(exc).__name__}: {exc}",
                            ) else 1

                        findings = payload.get("findings", []) if isinstance(payload, dict) else []
                        evidence_quotes = [
                            str(item.get("evidence_quote") or "").strip()
                            for item in findings
                            if isinstance(item, dict)
                            and str(item.get("evidence_quote") or "").strip()
                        ]
                        failures += 0 if print_result(
                            "content_json exists",
                            isinstance(payload, dict) and bool(payload),
                        ) else 1
                        failures += 0 if print_result(
                            "evidence_quote exists",
                            bool(evidence_quotes),
                        ) else 1
                        text_for_lang = " ".join(
                            [
                                str(payload.get("implications", "")),
                                str(payload.get("unresolved", "")),
                            ]
                            + [
                                str(item.get("point", ""))
                                for item in findings
                                if isinstance(item, dict)
                            ]
                        )
                        failures += 0 if print_result(
                            "Japanese output (simple check)",
                            contains_japanese(text_for_lang),
                        ) else 1

                    db.session.remove()
                    db.engine.dispose()
            finally:
                if app is not None:
                    try:
                        from models import db
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
        "canonical SQLite durable bytes unchanged",
        source_after == source_before,
        f"before={source_before!r} after={source_after!r}",
    ) else 1

    status_rc_after, status_after, status_err_after = git_status(repo_root)
    failures += 0 if print_result(
        "git status readable after provider smoke",
        status_rc_after == 0,
        status_err_after.strip(),
    ) else 1
    failures += 0 if print_result(
        "repository working-tree state unchanged",
        status_after == status_before,
        f"before={status_before!r} after={status_after!r}",
    ) else 1

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    mode = "injected provider" if provider_call is not None else "OpenAI provider"
    print(f"\nSummary: PASS ({mode}, disposable research fixture only)")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    import config

    return run_isolated_provider_smoke(
        source_db=Path(config.DATABASE_PATH),
        provider_call=None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
