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


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    failures = 0
    before_repo_state = repo_state(repo_root)

    status_proc = run_git(repo_root, "status", "--short")
    ok = status_proc.returncode == 0
    if not ok:
        detail = (status_proc.stderr or status_proc.stdout or "unknown git error").strip()
        failures += 0 if print_result("git status --short", ok, detail) else 1
    else:
        failures += 0 if print_result("git status --short", True) else 1

    env_proc = run_git(repo_root, "ls-files", "--error-unmatch", ".env")
    env_untracked = env_proc.returncode != 0
    failures += 0 if print_result(".env not tracked", env_untracked) else 1

    ls_proc = run_git(repo_root, "ls-files")
    if ls_proc.returncode == 0:
        tracked_files = [line.strip() for line in ls_proc.stdout.splitlines() if line.strip()]
        tracked_artifacts = [f for f in tracked_files if is_disallowed_tracked_path(f)]
        artifacts_ok = len(tracked_artifacts) == 0
        detail = "" if artifacts_ok else ", ".join(tracked_artifacts[:10])
        failures += 0 if print_result(
            "uploads/ outputs/ instance/ *.db logs/*.log not tracked", artifacts_ok, detail
        ) else 1
    else:
        detail = (ls_proc.stderr or ls_proc.stdout or "failed to read git ls-files").strip()
        failures += 0 if print_result(
            "uploads/ outputs/ instance/ *.db logs/*.log not tracked", False, detail
        ) else 1

    try:
        import config
        original_config = {
            "DATABASE_URI": config.DATABASE_URI,
            "UPLOAD_DIR": config.UPLOAD_DIR,
            "OUTPUT_DIR": config.OUTPUT_DIR,
        }
    except Exception as e:
        failures += 0 if print_result("config import", False, f"{type(e).__name__}: {e}") else 1
        print("\nSummary: FAIL")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        try:
            config.DATABASE_URI = f"sqlite:///{(tmp_dir / 'safe_smoke.db').as_posix()}"
            config.UPLOAD_DIR = str(tmp_dir / "uploads")
            config.OUTPUT_DIR = str(tmp_dir / "outputs")

            from app import create_app
            from models import db
            from models.project import Project
            from models.interview import Interview, Transcription
            from models.segment import Segment, UtteranceMapping
            from models.analysis import AIAnalysis

            failures += 0 if print_result("create_app import", True) else 1
            app = create_app()
            failures += 0 if print_result(
                "temporary database configured",
                app.config.get("SQLALCHEMY_DATABASE_URI") == config.DATABASE_URI,
                app.config.get("SQLALCHEMY_DATABASE_URI", ""),
            ) else 1
            failures += 0 if print_result(
                "temporary upload/output dirs configured",
                str(tmp_dir) in config.UPLOAD_DIR and str(tmp_dir) in config.OUTPUT_DIR,
            ) else 1
            client = app.test_client()

            try:
                r = client.get("/")
                failures += 0 if print_result("GET / returns 200", r.status_code == 200, f"status={r.status_code}") else 1
            except Exception as e:
                failures += 0 if print_result("GET / returns 200", False, f"{type(e).__name__}: {e}") else 1

            try:
                r = client.get("/settings")
                failures += 0 if print_result(
                    "GET /settings returns 200", r.status_code == 200, f"status={r.status_code}"
                ) else 1
            except Exception as e:
                failures += 0 if print_result(
                    "GET /settings returns 200", False, f"{type(e).__name__}: {e}"
                ) else 1

            with app.app_context():
                queryable = True
                details = []
                for model in (Project, Interview, Transcription, Segment, UtteranceMapping, AIAnalysis):
                    try:
                        model.query.limit(1).all()
                    except Exception as e:
                        queryable = False
                        details.append(f"{model.__name__}: {type(e).__name__}")
                failures += 0 if print_result(
                    "major models queryable",
                    queryable,
                    "; ".join(details),
                ) else 1
                db.session.remove()
                db.engine.dispose()
        except Exception as e:
            failures += 0 if print_result("temporary app smoke", False, f"{type(e).__name__}: {e}") else 1
        finally:
            config.DATABASE_URI = original_config["DATABASE_URI"]
            config.UPLOAD_DIR = original_config["UPLOAD_DIR"]
            config.OUTPUT_DIR = original_config["OUTPUT_DIR"]

    after_repo_state = repo_state(repo_root)
    failures += 0 if print_result(
        "repo instance/uploads/outputs state unchanged",
        before_repo_state == after_repo_state,
        f"before={before_repo_state}, after={after_repo_state}",
    ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
