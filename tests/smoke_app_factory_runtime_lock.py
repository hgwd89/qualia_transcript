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


def _child_code(repo_root: Path) -> str:
    return f"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, {str(repo_root)!r})
import config

root = Path(os.environ['QUALIA_FACTORY_TEST_ROOT']).resolve()
config.INSTANCE_DIR = str(root / 'instance')
config.DATABASE_PATH = str(root / 'instance' / 'factory.db')
config.DATABASE_URI = f"sqlite:///{{Path(config.DATABASE_PATH).as_posix()}}"
config.UPLOAD_DIR = str(root / 'uploads')
config.OUTPUT_DIR = str(root / 'outputs')
config.RUNTIME_LOCK_PATH = str(root / 'runtime.lock')

from app import create_app
create_app()
print('READY', flush=True)
time.sleep(30)
"""


def _start_factory_child(repo_root: Path, root: Path):
    env = dict(os.environ)
    env["QUALIA_FACTORY_TEST_ROOT"] = str(root)
    child = subprocess.Popen(
        [sys.executable, "-c", _child_code(repo_root)],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = child.stdout.readline().strip() if child.stdout else ""
    return child, ready


def _stop_child(child) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from services.runtime_lock import RuntimeLockError, runtime_lock

    with tempfile.TemporaryDirectory(prefix="qualia_factory_runtime_") as tmp:
        temp_root = Path(tmp)

        active_root = temp_root / "active"
        active_root.mkdir()
        active_lock = active_root / "runtime.lock"
        child = None
        try:
            child, ready = _start_factory_child(repo_root, active_root)
            failures += check(
                "canonical create_app factory acquired process-lifetime runtime lock",
                ready == "READY",
                ready or "no readiness line",
            )

            maintenance_blocked = False
            try:
                with runtime_lock("maintenance", active_lock):
                    pass
            except RuntimeLockError:
                maintenance_blocked = True
            failures += check(
                "factory runtime lock remains held after create_app returns",
                maintenance_blocked,
            )
        except Exception as exc:
            failures += check(
                "factory process-lifetime runtime lock",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if child is not None:
                _stop_child(child)

        try:
            with runtime_lock("maintenance", active_lock):
                pass
            failures += check(
                "factory runtime lock is released when process exits",
                True,
            )
        except Exception as exc:
            failures += check(
                "factory runtime lock is released when process exits",
                False,
                f"{type(exc).__name__}: {exc}",
            )

        blocked_root = temp_root / "blocked"
        blocked_root.mkdir()
        blocked_lock = blocked_root / "runtime.lock"
        env = dict(os.environ)
        env["QUALIA_FACTORY_TEST_ROOT"] = str(blocked_root)
        try:
            with runtime_lock("maintenance", blocked_lock):
                result = subprocess.run(
                    [sys.executable, "-c", _child_code(repo_root)],
                    cwd=str(repo_root),
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15,
                )

            output = (result.stdout or "") + (result.stderr or "")
            failures += check(
                "canonical create_app factory refuses maintenance overlap",
                result.returncode != 0 and "maintenance" in output.lower(),
                f"exit={result.returncode} output={output.strip()}",
            )
            failures += check(
                "factory refusal happens before database initialization",
                not (blocked_root / "instance" / "factory.db").exists(),
            )
            failures += check(
                "factory refusal happens before managed storage creation",
                not (blocked_root / "uploads").exists()
                and not (blocked_root / "outputs").exists(),
            )
        except Exception as exc:
            failures += check(
                "factory maintenance refusal",
                False,
                f"{type(exc).__name__}: {exc}",
            )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
