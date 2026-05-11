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
        or p.startswith("instance/")
        or p.endswith(".db")
        or p.endswith(".sqlite3")
        or p.endswith(".db-journal")
        or p.endswith(".sqlite3-journal")
        or (p.startswith("logs/") and p.endswith(".log"))
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    failures = 0

    status_proc = run_git(repo_root, "status", "--short")
    ok = status_proc.returncode == 0
    detail = "" if ok else (status_proc.stderr or status_proc.stdout or "unknown git error").strip()
    failures += 0 if print_result("git status --short", ok, detail) else 1

    env_proc = run_git(repo_root, "ls-files", "--error-unmatch", ".env")
    failures += 0 if print_result(".env not tracked", env_proc.returncode != 0) else 1

    ls_proc = run_git(repo_root, "ls-files")
    if ls_proc.returncode == 0:
        tracked_files = [line.strip() for line in ls_proc.stdout.splitlines() if line.strip()]
        tracked_artifacts = [f for f in tracked_files if is_disallowed_tracked_path(f)]
        artifacts_ok = len(tracked_artifacts) == 0
        detail = "" if artifacts_ok else ", ".join(tracked_artifacts[:10])
        failures += 0 if print_result(
            "uploads/ outputs/ instance/ *.db logs/*.log not tracked",
            artifacts_ok,
            detail,
        ) else 1
    else:
        detail = (ls_proc.stderr or ls_proc.stdout or "failed to read git ls-files").strip()
        failures += 0 if print_result(
            "uploads/ outputs/ instance/ *.db logs/*.log not tracked",
            False,
            detail,
        ) else 1

    try:
        import config
        from app import create_app
        from models import db
        from models.analysis import AIAnalysis
        from models.generated_file import GeneratedFile
        from models.interview import Interview, MediaFile, Transcription
        from models.interview_flow import InterviewFlow, InterviewFlowQuestion, InterviewFlowSection
        from models.participant import Participant, ParticipantAttribute
        from models.project import Project
        from models.quote_candidate import QuoteCandidate
        from models.review_item import ReviewItem
        from models.segment import Segment, UtteranceMapping
        from models.segment_flag import SegmentFlag
        from models.speaker_assignment import SpeakerAssignment

        failures += 0 if print_result("create_app import", True) else 1
    except Exception as e:
        failures += 0 if print_result("create_app import", False, f"{type(e).__name__}: {e}") else 1
        print("\nSummary: FAIL")
        return 1

    original_database_uri = config.DATABASE_URI
    original_upload_dir = config.UPLOAD_DIR
    original_output_dir = config.OUTPUT_DIR

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        config.DATABASE_URI = f"sqlite:///{(tmp_root / 'safe_smoke.db').as_posix()}"
        config.UPLOAD_DIR = str(tmp_root / "uploads")
        config.OUTPUT_DIR = str(tmp_root / "outputs")
        try:
            app = create_app()
            client = app.test_client()

            failures += 0 if print_result(
                "temporary database configured",
                config.DATABASE_URI.startswith("sqlite:///") and config.DATABASE_URI.endswith("/safe_smoke.db"),
                config.DATABASE_URI,
            ) else 1
            failures += 0 if print_result(
                "temporary upload/output dirs configured",
                str(tmp_root) in config.UPLOAD_DIR and str(tmp_root) in config.OUTPUT_DIR,
            ) else 1

            try:
                r = client.get("/")
                failures += 0 if print_result(
                    "GET / returns 200",
                    r.status_code == 200,
                    f"status={r.status_code}",
                ) else 1
            except Exception as e:
                failures += 0 if print_result("GET / returns 200", False, f"{type(e).__name__}: {e}") else 1

            try:
                r = client.get("/settings")
                failures += 0 if print_result(
                    "GET /settings returns 200",
                    r.status_code == 200,
                    f"status={r.status_code}",
                ) else 1
            except Exception as e:
                failures += 0 if print_result(
                    "GET /settings returns 200",
                    False,
                    f"{type(e).__name__}: {e}",
                ) else 1

            model_classes = [
                Project,
                Participant,
                ParticipantAttribute,
                Interview,
                MediaFile,
                Transcription,
                InterviewFlow,
                InterviewFlowSection,
                InterviewFlowQuestion,
                Segment,
                UtteranceMapping,
                SegmentFlag,
                SpeakerAssignment,
                AIAnalysis,
                GeneratedFile,
                QuoteCandidate,
                ReviewItem,
            ]
            with app.app_context():
                query_ok = True
                query_error = ""
                try:
                    for model_class in model_classes:
                        model_class.query.count()
                except Exception as e:
                    query_ok = False
                    query_error = f"{type(e).__name__}: {e}"
                failures += 0 if print_result("major models queryable", query_ok, query_error) else 1
                db.session.remove()
                db.engine.dispose()
        finally:
            config.DATABASE_URI = original_database_uri
            config.UPLOAD_DIR = original_upload_dir
            config.OUTPUT_DIR = original_output_dir

    failures += 0 if print_result(
        "repo instance dir not created",
        not (repo_root / "instance").exists(),
    ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
