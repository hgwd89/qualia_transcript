import json
import subprocess
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "scripts" / "run_integrated_analysis.py"
SERVICE = ROOT_DIR / "services" / "integrated_analysis.py"


def print_result(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    if detail:
        print(f"[{status}] {name}: {detail}")
    else:
        print(f"[{status}] {name}")
    return ok


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def parse_json_stdout(proc: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {}


def main() -> int:
    failures = 0

    service_text = SERVICE.read_text(encoding="utf-8-sig")
    header = service_text.split("AI_SUMMARY_SCHEMA", 1)[0]
    failures += 0 if print_result(
        "no top-level ai_client import",
        "from services.ai_client" not in header,
    ) else 1

    failures += 0 if print_result(
        "lazy ai_client import exists",
        "from services.ai_client import MODEL as integrator_model" in service_text
        and "from services.ai_client import call_structured" in service_text,
    ) else 1

    help_proc = run_cli("--help")
    failures += 0 if print_result(
        "help command succeeds",
        help_proc.returncode == 0,
        f"returncode={help_proc.returncode}",
    ) else 1
    failures += 0 if print_result(
        "help exposes --ai",
        "--ai" in help_proc.stdout,
    ) else 1

    conflict_proc = run_cli("--interview-id", "10", "--ai", "--no-ai")
    conflict_json = parse_json_stdout(conflict_proc)
    failures += 0 if print_result(
        "ai/no-ai conflict returns code 2",
        conflict_proc.returncode == 2,
        f"returncode={conflict_proc.returncode}",
    ) else 1
    failures += 0 if print_result(
        "ai/no-ai conflict returns ArgumentError",
        conflict_json.get("ok") is False
        and conflict_json.get("error_type") == "ArgumentError"
        and "--ai and --no-ai cannot be used together" in conflict_json.get("error_message", ""),
        f"stdout={conflict_proc.stdout.strip()}",
    ) else 1

    save_proc = run_cli("--interview-id", "10", "--save")
    save_json = parse_json_stdout(save_proc)
    failures += 0 if print_result(
        "save remains disabled",
        save_proc.returncode == 1,
        f"returncode={save_proc.returncode}",
    ) else 1
    failures += 0 if print_result(
        "save returns NotImplementedError",
        save_json.get("ok") is False
        and save_json.get("error_type") == "NotImplementedError",
        f"stdout={save_proc.stdout.strip()}",
    ) else 1

    if failures == 0:
        print("\nSummary: PASS")
        return 0

    print(f"\nSummary: FAIL ({failures} checks failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())