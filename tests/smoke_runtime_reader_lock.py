import os
import subprocess
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import config
    from services.runtime_lock import RuntimeLockError, runtime_lock
    from scripts import recover_processing_job, run_integrated_analysis

    original_lock_path = config.RUNTIME_LOCK_PATH
    original_argv = list(sys.argv)

    with tempfile.TemporaryDirectory(prefix="qualia_runtime_reader_") as tmp:
        lock_path = Path(tmp) / "runtime.lock"
        config.RUNTIME_LOCK_PATH = str(lock_path)
        env = dict(os.environ)
        env["QUALIA_RUNTIME_LOCK_PATH"] = str(lock_path)

        try:
            with runtime_lock("reader", lock_path):
                blocked = False
                try:
                    with runtime_lock("maintenance", lock_path):
                        pass
                except RuntimeLockError:
                    blocked = True
                failures += check(
                    "reader shared lock blocks exclusive maintenance",
                    blocked,
                )

            with runtime_lock("maintenance", lock_path):
                launcher = subprocess.run(
                    [
                        sys.executable,
                        "scripts/run_with_runtime_reader.py",
                        "tests/smoke_safe.py",
                    ],
                    cwd=str(repo_root),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                failures += check(
                    "reader launcher refuses target execution during maintenance",
                    launcher.returncode == 3,
                    f"exit={launcher.returncode} stdout={launcher.stdout.strip()}",
                )

                sys.argv = ["recover_processing_job.py", "--job-id", "1"]
                recovery_exit = recover_processing_job.main()
                failures += check(
                    "job inspection refuses before opening DB during maintenance",
                    recovery_exit == 3,
                    f"exit={recovery_exit}",
                )

                sys.argv = [
                    "run_integrated_analysis.py",
                    "--interview-id",
                    "1",
                    "--dry-run",
                    "--no-ai",
                ]
                integrated_exit = run_integrated_analysis.main()
                failures += check(
                    "integrated dry-run refuses before opening DB during maintenance",
                    integrated_exit == 3,
                    f"exit={integrated_exit}",
                )
        except Exception as exc:
            failures += check(
                "runtime reader exclusion",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            sys.argv = original_argv
            config.RUNTIME_LOCK_PATH = original_lock_path

    readiness_wrapper = (repo_root / "scripts" / "check_production_readiness.ps1").read_text(
        encoding="utf-8"
    )
    local_integrity_wrapper = (repo_root / "scripts" / "check_local_data_integrity.ps1").read_text(
        encoding="utf-8"
    )
    failures += check(
        "production readiness wrapper uses reader launcher",
        "run_with_runtime_reader.py" in readiness_wrapper,
    )
    failures += check(
        "manual local-data wrapper uses reader launcher",
        "run_with_runtime_reader.py" in local_integrity_wrapper,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
