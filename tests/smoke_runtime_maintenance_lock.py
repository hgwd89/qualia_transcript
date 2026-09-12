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

    from services.runtime_lock import RuntimeLockError, runtime_lock

    with tempfile.TemporaryDirectory(prefix="qualia_runtime_lock_") as tmp:
        lock_path = Path(tmp) / "runtime.lock"

        try:
            with runtime_lock("maintenance", lock_path):
                with runtime_lock("maintenance", lock_path):
                    pass
                blocked = False
                try:
                    with runtime_lock("app", lock_path):
                        pass
                except RuntimeLockError:
                    blocked = True
                failures += check(
                    "same-process maintenance reentry allowed but role switch blocked",
                    blocked,
                )
        except Exception as exc:
            failures += check(
                "same-process runtime lock contract",
                False,
                f"{type(exc).__name__}: {exc}",
            )

        child_code = (
            "import sys,time; "
            f"sys.path.insert(0, {str(repo_root)!r}); "
            "from services.runtime_lock import runtime_lock; "
            f"cm=runtime_lock('app', {str(lock_path)!r}); "
            "cm.__enter__(); print('LOCKED', flush=True); time.sleep(30)"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", child_code],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready = child.stdout.readline().strip() if child.stdout else ""
            failures += check(
                "child app process acquired runtime lock",
                ready == "LOCKED",
                ready or "no readiness line",
            )

            blocked = False
            try:
                with runtime_lock("maintenance", lock_path):
                    pass
            except RuntimeLockError:
                blocked = True
            failures += check(
                "maintenance is refused while app process owns runtime lock",
                blocked,
            )
        finally:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

        try:
            with runtime_lock("maintenance", lock_path):
                pass
            failures += check(
                "maintenance lock is available after app process exits",
                True,
            )
        except Exception as exc:
            failures += check(
                "maintenance lock is available after app process exits",
                False,
                f"{type(exc).__name__}: {exc}",
            )

    app_text = (repo_root / "app.py").read_text(encoding="utf-8")
    backup_text = (repo_root / "scripts" / "backup_local_data.py").read_text(encoding="utf-8")
    restore_text = (repo_root / "scripts" / "restore_local_data.py").read_text(encoding="utf-8")
    failures += check(
        "local app lifetime owns app runtime lock",
        'with runtime_lock("app")' in app_text,
    )
    failures += check(
        "backup CLI owns maintenance runtime lock",
        'with runtime_lock("maintenance")' in backup_text,
    )
    failures += check(
        "applied restore CLI owns maintenance runtime lock",
        'with runtime_lock("maintenance")' in restore_text,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
