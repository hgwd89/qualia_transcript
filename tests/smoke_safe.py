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


def run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def is_disallowed_tracked_path(path: str) -> bool:
    p = path.replace("\\", "/")
    return (
        p.startswith("uploads/")
        or p.startswith("outputs/")
        or p.startswith("raw_transcripts/")
        or p.endswith(".db")
        or p.endswith(".db-journal")
        or p.endswith(".sqlite3-journal")
        or (p.startswith("logs/") and p.endswith(".log"))
    )


def repo_state(repo_root: Path) -> dict[str, bool]:
    return {
        "instance": (repo_root / "instance").exists(),
        "uploads": (repo_root / "uploads").exists(),
        "outputs": (repo_root / "outputs").exists(),
    }


def add_failure(failures: int, name: str, ok: bool, detail: str = "") -> int:
    return failures + (0 if print_result(name, ok, detail) else 1)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0

    status_proc = run_git(repo_root, "status", "--short")
    status_ok = status_proc.returncode == 0
    failures = add_failure(
        failures,
        "git status --short",
        status_ok,
        "" if status_ok else (status_proc.stderr or status_proc.stdout or "unknown git error").strip(),
    )

    env_proc = run_git(repo_root, "ls-files", "--error-unmatch", ".env")
    failures = add_failure(failures, ".env not tracked", env_proc.returncode != 0)

    ls_proc = run_git(repo_root, "ls-files")
    if ls_proc.returncode == 0:
        tracked_files = [line.strip() for line in ls_proc.stdout.splitlines() if line.strip()]
        tracked_artifacts = [f for f in tracked_files if is_disallowed_tracked_path(f)]
        failures = add_failure(
            failures,
            "uploads/ outputs/ raw_transcripts/ *.db logs/*.log not tracked",
            len(tracked_artifacts) == 0,
            "" if not tracked_artifacts else ", ".join(tracked_artifacts[:10]),
        )
    else:
        failures = add_failure(
            failures,
            "uploads/ outputs/ raw_transcripts/ *.db logs/*.log not tracked",
            False,
            (ls_proc.stderr or ls_proc.stdout or "failed to read git ls-files").strip(),
        )

    before_state = repo_state(repo_root)

    try:
        import config

        original_config = {
            "DATABASE_URI": config.DATABASE_URI,
            "UPLOAD_DIR": config.UPLOAD_DIR,
            "OUTPUT_DIR": config.OUTPUT_DIR,
        }
    except Exception as e:
        failures = add_failure(failures, "config import", False, f"{type(e).__name__}: {e}")
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1

    try:
        with tempfile.TemporaryDirectory(prefix="qualia_safe_smoke_") as tmp:
            tmp_dir = Path(tmp)
            config.DATABASE_URI = f"sqlite:///{(tmp_dir / 'safe_smoke.db').as_posix()}"
            config.UPLOAD_DIR = str(tmp_dir / "uploads")
            config.OUTPUT_DIR = str(tmp_dir / "outputs")

            try:
                from app import create_app
                from models.analysis import AIAnalysis
                from models.generated_file import GeneratedFile
                from models.interview import Interview, Transcription
                from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
                from models.participant import Participant
                from models.project import Project
                from models.segment import Segment, UtteranceMapping
                from models.segment_flag import SegmentFlag
                from models.setting import AppSetting
                from models.speaker_assignment import SpeakerAssignment
                from models import db

                failures = add_failure(failures, "create_app import", True)
            except Exception as e:
                failures = add_failure(
                    failures, "create_app import", False, f"{type(e).__name__}: {e}"
                )
                print(f"\nSummary: FAIL ({failures} checks failed)")
                return 1

            app = create_app()
            client = app.test_client()

            try:
                response = client.get("/")
                failures = add_failure(
                    failures,
                    "GET / returns 200",
                    response.status_code == 200,
                    f"status={response.status_code}",
                )
            except Exception as e:
                failures = add_failure(
                    failures, "GET / returns 200", False, f"{type(e).__name__}: {e}"
                )

            try:
                response = client.get("/settings")
                failures = add_failure(
                    failures,
                    "GET /settings returns 200",
                    response.status_code == 200,
                    f"status={response.status_code}",
                )
            except Exception as e:
                failures = add_failure(
                    failures, "GET /settings returns 200", False, f"{type(e).__name__}: {e}"
                )

            with app.app_context():
                model_checks = [
                    ("Project query", Project),
                    ("Participant query", Participant),
                    ("InterviewFlow query", InterviewFlow),
                    ("InterviewFlowSection query", InterviewFlowSection),
                    ("InterviewFlowQuestion query", InterviewFlowQuestion),
                    ("Interview query", Interview),
                    ("Transcription query", Transcription),
                    ("Segment query", Segment),
                    ("UtteranceMapping query", UtteranceMapping),
                    ("SegmentFlag query", SegmentFlag),
                    ("SpeakerAssignment query", SpeakerAssignment),
                    ("AIAnalysis query", AIAnalysis),
                    ("GeneratedFile query", GeneratedFile),
                    ("AppSetting query", AppSetting),
                ]

                try:
                    for check_name, model in model_checks:
                        model.query.count()
                        failures = add_failure(failures, check_name, True)
                except Exception as e:
                    failures = add_failure(
                        failures,
                        "model query checks",
                        False,
                        f"{type(e).__name__}: {e}",
                    )
                finally:
                    db.session.remove()
                    db.engine.dispose()

            db_path = tmp_dir / "safe_smoke.db"
            failures = add_failure(failures, "temporary database used", db_path.exists())
            failures = add_failure(failures, "temporary upload dir configured", Path(config.UPLOAD_DIR).is_relative_to(tmp_dir))
            failures = add_failure(failures, "temporary output dir configured", Path(config.OUTPUT_DIR).is_relative_to(tmp_dir))
    except Exception as e:
        failures = add_failure(failures, "safe smoke temporary app", False, f"{type(e).__name__}: {e}")
    finally:
        config.DATABASE_URI = original_config["DATABASE_URI"]
        config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
        config.OUTPUT_DIR = original_config["OUTPUT_DIR"]

    after_state = repo_state(repo_root)
    failures = add_failure(
        failures,
        "repo instance/uploads/outputs state unchanged",
        before_state == after_state,
        f"before={before_state} after={after_state}" if before_state != after_state else "",
    )

    if failures == 0:
        print("\nSummary: PASS")
        return 0

    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
