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
    from services.runtime_lock import runtime_lock
    from scripts import recover_processing_job, run_semantic_analysis

    original_lock_path = config.RUNTIME_LOCK_PATH
    original_argv = list(sys.argv)

    with tempfile.TemporaryDirectory(prefix="qualia_admin_cli_lock_") as tmp:
        lock_path = Path(tmp) / "runtime.lock"
        config.RUNTIME_LOCK_PATH = str(lock_path)
        try:
            with runtime_lock("maintenance", lock_path):
                sys.argv = [
                    "run_semantic_analysis.py",
                    "--interview-id",
                    "1",
                    "--dry-run",
                    "--no-ai",
                ]
                semantic_exit = run_semantic_analysis.main()
                failures += check(
                    "semantic CLI refuses startup while maintenance is active",
                    semantic_exit == 3,
                    f"exit={semantic_exit}",
                )

                recovery_exit = recover_processing_job._apply_recovery(1, {})
                failures += check(
                    "explicit job recovery refuses write while maintenance is active",
                    recovery_exit == 3,
                    f"exit={recovery_exit}",
                )
        except Exception as exc:
            failures += check(
                "admin CLI maintenance exclusion",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            sys.argv = original_argv
            config.RUNTIME_LOCK_PATH = original_lock_path

    semantic_text = (repo_root / "scripts" / "run_semantic_analysis.py").read_text(
        encoding="utf-8"
    )
    recovery_text = (repo_root / "scripts" / "recover_processing_job.py").read_text(
        encoding="utf-8"
    )
    failures += check(
        "semantic CLI holds shared runtime lock around create_app",
        'with runtime_lock("worker")' in semantic_text
        and semantic_text.index('with runtime_lock("worker")')
        < semantic_text.index("app = create_app()"),
    )
    failures += check(
        "recovery inspection remains read-only before apply confirmation",
        'if not args.apply:' in recovery_text
        and 'return _apply_recovery(args.job_id, inspected_generation)' in recovery_text,
    )
    failures += check(
        "explicit recovery write holds shared runtime lock",
        'with runtime_lock("worker")' in recovery_text,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
