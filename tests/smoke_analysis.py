import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
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


def contains_japanese(text: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text or ""))


def _read_stored_openai_secret(source_db: Path) -> str | None:
    """Read only the stored API credential needed by the provider smoke.

    The research database is never opened through SQLAlchemy by this test and is
    never written. On Windows the copied value may be a current-user DPAPI blob;
    it remains decryptable from the temporary fixture under the same account.
    """
    if not source_db.is_file():
        return None
    uri = f"file:{source_db.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
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


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    failures += 0 if print_result(
        "git status --short before",
        run_git_status(repo_root, "before"),
    ) else 1

    try:
        import config
    except Exception as exc:
        print_result("config import", False, f"{type(exc).__name__}: {exc}")
        return 1

    source_db = Path(config.DATABASE_PATH).resolve()
    stored_openai_secret = _read_stored_openai_secret(source_db)

    original_config = {
        "DATABASE_URI": config.DATABASE_URI,
        "UPLOAD_DIR": config.UPLOAD_DIR,
        "OUTPUT_DIR": config.OUTPUT_DIR,
        "BACKUP_DIR": config.BACKUP_DIR,
        "RUNTIME_LOCK_PATH": config.RUNTIME_LOCK_PATH,
    }

    # OpenAI 到達を阻害する壊れたプロキシを、このプロセス限定で除去。
    for key in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"]:
        os.environ.pop(key, None)

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
                import services.analyzer as analyzer
            except Exception as exc:
                failures += 0 if print_result("imports", False, f"{type(exc).__name__}: {exc}") else 1
                return 1

            app = create_app()
            app.config["TESTING"] = True

            with app.app_context():
                # Preserve the operator's configured provider credential without
                # copying any research rows into the temporary fixture.
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
                    status="mapped",
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
                    "analysis fixture uses temporary database",
                    temp_db.resolve() != source_db,
                    f"temp={temp_db}",
                ) else 1
                failures += 0 if print_result(
                    "fixture interview is self-contained",
                    interview_id > 0 and interview.project_id == project.id,
                    f"interview_id={interview_id}",
                ) else 1

                status_before = interview.status
                count_before = AIAnalysis.query.count()

                counter = {"n": 0}
                original_call = analyzer.call_structured

                def wrapped_call(*args, **kwargs):
                    counter["n"] += 1
                    return original_call(*args, **kwargs)

                analyzer.call_structured = wrapped_call

                analyze_ok = True
                error_detail = ""
                saved_id = None
                try:
                    # This is an isolated provider smoke, not a production write
                    # path. The no-op guard deliberately stands in for the durable
                    # result-write reservation because the DB is disposable.
                    analysis = analyzer.analyze_interview_summary(
                        interview_id,
                        result_write_guard=lambda: None,
                    )
                    saved_id = analysis.id
                except Exception as exc:
                    analyze_ok = False
                    error_detail = f"{type(exc).__name__}: {exc}"
                finally:
                    analyzer.call_structured = original_call

                failures += 0 if print_result(
                    "analyze_interview_summary fixture executed once",
                    analyze_ok,
                    error_detail,
                ) else 1
                failures += 0 if print_result(
                    "OpenAI API call count == 1",
                    counter["n"] == 1,
                    f"count={counter['n']}",
                ) else 1

                count_after = AIAnalysis.query.count()
                failures += 0 if print_result(
                    "temporary AIAnalysis count increased",
                    count_after == count_before + 1,
                    f"before={count_before}, after={count_after}",
                ) else 1

                interview_after = db.session.get(Interview, interview_id)
                status_after = interview_after.status if interview_after else None
                failures += 0 if print_result(
                    "temporary Interview.status becomes analyzed",
                    status_after == "analyzed",
                    f"before={status_before}, after={status_after}",
                ) else 1

                latest = db.session.get(AIAnalysis, saved_id) if saved_id else None
                failures += 0 if print_result("temporary AIAnalysis exists", latest is not None) else 1

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
                            "content_json parse", False, f"{type(exc).__name__}: {exc}"
                        ) else 1

                    findings = payload.get("findings", []) if isinstance(payload, dict) else []
                    failures += 0 if print_result(
                        "content_json exists",
                        isinstance(payload, dict) and bool(payload),
                    ) else 1
                    evidence_quotes = [
                        (item.get("evidence_quote") or "").strip()
                        for item in findings
                        if isinstance(item, dict) and (item.get("evidence_quote") or "").strip()
                    ]
                    failures += 0 if print_result(
                        "evidence_quote exists",
                        bool(evidence_quotes),
                    ) else 1

                    text_for_lang = " ".join(
                        [payload.get("implications", ""), payload.get("unresolved", "")]
                        + [str(item.get("point", "")) for item in findings if isinstance(item, dict)]
                    )
                    failures += 0 if print_result(
                        "Japanese output (simple check)",
                        contains_japanese(text_for_lang),
                    ) else 1

                db.session.remove()
                db.engine.dispose()
    finally:
        config.DATABASE_URI = original_config["DATABASE_URI"]
        config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
        config.OUTPUT_DIR = original_config["OUTPUT_DIR"]
        config.BACKUP_DIR = original_config["BACKUP_DIR"]
        config.RUNTIME_LOCK_PATH = original_config["RUNTIME_LOCK_PATH"]

    failures += 0 if print_result(
        "git status --short after",
        run_git_status(repo_root, "after"),
    ) else 1

    if failures == 0:
        print("\nSummary: PASS (OpenAI provider, temporary fixture only)")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
