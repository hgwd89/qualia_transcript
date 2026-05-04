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


def is_ignored(path: str, repo_root: Path) -> bool:
    proc = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode == 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    failures = 0
    failures += 0 if print_result(
        "git status --short before", run_git_status(repo_root, "before")
    ) else 1

    try:
        import config
        from app import create_app
        from models.generated_file import GeneratedFile
        from services.report_verbatim import generate_verbatim
        from services.report_formatted import generate_formatted_sheet
    except Exception as e:
        failures += 0 if print_result("imports", False, f"{type(e).__name__}: {e}") else 1
        print("\nSummary: FAIL")
        return 1

    app = create_app()
    with app.app_context():
        interview_id = 4
        project_id = 2

        before_count = GeneratedFile.query.count()

        verbatim_ok = True
        formatted_ok = True
        verbatim_error = ""
        formatted_error = ""
        gf_verbatim = None
        gf_formatted = None

        try:
            gf_verbatim = generate_verbatim(interview_id)
        except Exception as e:
            verbatim_ok = False
            verbatim_error = f"{type(e).__name__}: {e}"

        try:
            gf_formatted = generate_formatted_sheet(project_id)
        except Exception as e:
            formatted_ok = False
            formatted_error = f"{type(e).__name__}: {e}"

        failures += 0 if print_result("generate_verbatim(4)", verbatim_ok, verbatim_error) else 1
        failures += 0 if print_result("generate_formatted_sheet(2)", formatted_ok, formatted_error) else 1

        after_count = GeneratedFile.query.count()
        failures += 0 if print_result(
            "GeneratedFile count increased by 2",
            after_count == before_count + 2,
            f"before={before_count}, after={after_count}",
        ) else 1

        if gf_verbatim is not None:
            v_path = Path(config.OUTPUT_DIR) / gf_verbatim.stored_path
            failures += 0 if print_result(
                "verbatim file exists", v_path.is_file(), str(v_path)
            ) else 1
            failures += 0 if print_result(
                "verbatim extension is .docx",
                gf_verbatim.original_filename.lower().endswith(".docx"),
                gf_verbatim.original_filename,
            ) else 1
            failures += 0 if print_result(
                "verbatim output path ignored by git",
                is_ignored(str(Path("outputs") / gf_verbatim.stored_path.replace("\\", "/")), repo_root),
                gf_verbatim.stored_path,
            ) else 1
            print(f"[INFO] generated_verbatim_file_id={gf_verbatim.id}")

        if gf_formatted is not None:
            f_path = Path(config.OUTPUT_DIR) / gf_formatted.stored_path
            failures += 0 if print_result(
                "formatted file exists", f_path.is_file(), str(f_path)
            ) else 1
            failures += 0 if print_result(
                "formatted extension is .xlsx",
                gf_formatted.original_filename.lower().endswith(".xlsx"),
                gf_formatted.original_filename,
            ) else 1
            failures += 0 if print_result(
                "formatted output path ignored by git",
                is_ignored(str(Path("outputs") / gf_formatted.stored_path.replace("\\", "/")), repo_root),
                gf_formatted.stored_path,
            ) else 1
            print(f"[INFO] generated_formatted_file_id={gf_formatted.id}")

        print("[INFO] cleanup_performed=false (safety policy: no DB/file deletion)")

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
