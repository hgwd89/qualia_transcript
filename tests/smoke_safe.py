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
        or p.endswith(".db")
        or (p.startswith("logs/") and p.endswith(".log"))
    )


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    failures = 0

    # 1) git status --short can be retrieved
    status_proc = run_git(repo_root, "status", "--short")
    ok = status_proc.returncode == 0
    if not ok:
        detail = (status_proc.stderr or status_proc.stdout or "unknown git error").strip()
        failures += 0 if print_result("git status --short", ok, detail) else 1
    else:
        failures += 0 if print_result("git status --short", True) else 1

    # 2) .env is not tracked
    env_proc = run_git(repo_root, "ls-files", "--error-unmatch", ".env")
    env_untracked = env_proc.returncode != 0
    failures += 0 if print_result(".env not tracked", env_untracked) else 1

    # 3) uploads/ outputs/ *.db logs/*.log are not tracked
    ls_proc = run_git(repo_root, "ls-files")
    tracked_artifacts = []
    if ls_proc.returncode == 0:
        tracked_files = [line.strip() for line in ls_proc.stdout.splitlines() if line.strip()]
        tracked_artifacts = [f for f in tracked_files if is_disallowed_tracked_path(f)]
        artifacts_ok = len(tracked_artifacts) == 0
        detail = "" if artifacts_ok else ", ".join(tracked_artifacts[:10])
        failures += 0 if print_result(
            "uploads/ outputs/ *.db logs/*.log not tracked", artifacts_ok, detail
        ) else 1
    else:
        detail = (ls_proc.stderr or ls_proc.stdout or "failed to read git ls-files").strip()
        failures += 0 if print_result(
            "uploads/ outputs/ *.db logs/*.log not tracked", False, detail
        ) else 1

    # App-level checks
    try:
        from app import create_app
        from models.project import Project
        from models.interview import Interview, Transcription
        from models.segment import Segment, UtteranceMapping
        from models.analysis import AIAnalysis

        failures += 0 if print_result("create_app import", True) else 1
    except Exception as e:
        failures += 0 if print_result("create_app import", False, f"{type(e).__name__}: {e}") else 1
        print("\nSummary: FAIL")
        return 1

    app = create_app()
    client = app.test_client()

    # 4) GET / == 200
    try:
        r = client.get("/")
        failures += 0 if print_result("GET / returns 200", r.status_code == 200, f"status={r.status_code}") else 1
    except Exception as e:
        failures += 0 if print_result("GET / returns 200", False, f"{type(e).__name__}: {e}") else 1

    # 5) GET /settings == 200
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
        # 6) project_id=2 exists
        project = Project.query.get(2)
        failures += 0 if print_result("project_id=2 exists", project is not None) else 1

        # 7) interview_id=4 exists
        interview = Interview.query.get(4)
        failures += 0 if print_result("interview_id=4 exists", interview is not None) else 1

        # 8) transcription_id=8 exists
        transcription = Transcription.query.get(8)
        failures += 0 if print_result("transcription_id=8 exists", transcription is not None) else 1

        # 9) segment_id=40 exists
        segment = Segment.query.get(40)
        failures += 0 if print_result("segment_id=40 exists", segment is not None) else 1

        # 10) segment text matches expected
        expected_text = "大学の時に上京しました。"
        actual_text = (segment.text if segment else "").strip()
        failures += 0 if print_result(
            "segment_id=40 text matches",
            actual_text == expected_text,
            f"text={actual_text}" if segment else "segment missing",
        ) else 1

        # 11) mapping_count >= 1 for interview_id=4
        mapping_count = 0
        if interview is not None:
            seg_ids = [s.id for s in Segment.query.filter_by(interview_id=4).all()]
            if seg_ids:
                mapping_count = UtteranceMapping.query.filter(
                    UtteranceMapping.segment_id.in_(seg_ids)
                ).count()
        failures += 0 if print_result(
            "mapping_count >= 1", mapping_count >= 1, f"count={mapping_count}"
        ) else 1

        # 12) per_question analysis >= 1
        analysis_count = AIAnalysis.query.filter_by(
            interview_id=4, analysis_type="per_question"
        ).count()
        failures += 0 if print_result(
            "per_question analysis >= 1", analysis_count >= 1, f"count={analysis_count}"
        ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0
    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
