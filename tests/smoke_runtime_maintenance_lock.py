import subprocess
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def start_shared_child(repo_root: Path, lock_path: Path, role: str):
    child_code = (
        "import sys,time; "
        f"sys.path.insert(0, {str(repo_root)!r}); "
        "from services.runtime_lock import runtime_lock; "
        f"cm=runtime_lock({role!r}, {str(lock_path)!r}); "
        "cm.__enter__(); print('LOCKED', flush=True); time.sleep(30)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready = child.stdout.readline().strip() if child.stdout else ""
    return child, ready


def stop_child(child) -> None:
    child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


def maintenance_blocked(runtime_lock, RuntimeLockError, lock_path: Path) -> bool:
    try:
        with runtime_lock("maintenance", lock_path):
            pass
    except RuntimeLockError:
        return True
    return False


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
                    "same-process maintenance reentry allowed but shared-mode switch blocked",
                    blocked,
                )
        except Exception as exc:
            failures += check(
                "same-process runtime lock contract",
                False,
                f"{type(exc).__name__}: {exc}",
            )

        class NestedFailure(RuntimeError):
            pass

        nested_propagated = False
        try:
            with runtime_lock("maintenance", lock_path):
                with runtime_lock("maintenance", lock_path):
                    raise NestedFailure("must escape nested context")
        except NestedFailure as exc:
            nested_propagated = str(exc) == "must escape nested context"
        failures += check(
            "nested same-mode runtime lock preserves with-body exceptions",
            nested_propagated,
        )

        reacquired_after_failure = False
        try:
            with runtime_lock("maintenance", lock_path):
                reacquired_after_failure = True
        except Exception:
            reacquired_after_failure = False
        failures += check(
            "nested exception unwinds reference counts and releases the outer lock",
            reacquired_after_failure,
        )

        app_child = worker_child = None
        try:
            app_child, app_ready = start_shared_child(repo_root, lock_path, "app")
            failures += check(
                "child app process acquired shared runtime lock",
                app_ready == "LOCKED",
                app_ready or "no readiness line",
            )

            worker_child, worker_ready = start_shared_child(repo_root, lock_path, "worker")
            failures += check(
                "detached worker can coexist with app shared lock",
                worker_ready == "LOCKED",
                worker_ready or "no readiness line",
            )
            failures += check(
                "maintenance is refused while app/worker shared locks are active",
                maintenance_blocked(runtime_lock, RuntimeLockError, lock_path),
            )

            stop_child(app_child)
            app_child = None
            failures += check(
                "maintenance remains refused while detached worker outlives app",
                maintenance_blocked(runtime_lock, RuntimeLockError, lock_path),
            )

            stop_child(worker_child)
            worker_child = None
            with runtime_lock("maintenance", lock_path):
                pass
            failures += check(
                "maintenance becomes available only after app and worker exit",
                True,
            )
        except Exception as exc:
            failures += check(
                "cross-process shared/exclusive runtime lock contract",
                False,
                f"{type(exc).__name__}: {exc}",
            )
        finally:
            if app_child is not None:
                stop_child(app_child)
            if worker_child is not None:
                stop_child(worker_child)

    app_text = (repo_root / "app.py").read_text(encoding="utf-8")
    worker_text = (repo_root / "scripts" / "run_processing_job.py").read_text(encoding="utf-8")
    backup_text = (repo_root / "scripts" / "backup_local_data.py").read_text(encoding="utf-8")
    restore_text = (repo_root / "scripts" / "restore_local_data.py").read_text(encoding="utf-8")
    runtime_lock_text = (repo_root / "services" / "runtime_lock.py").read_text(encoding="utf-8")
    failures += check(
        "local app lifetime owns shared runtime lock",
        'with runtime_lock("app")' in app_text,
    )
    failures += check(
        "detached durable worker lifetime owns shared runtime lock",
        'with runtime_lock("worker")' in worker_text,
    )
    failures += check(
        "backup CLI owns exclusive maintenance runtime lock",
        'with runtime_lock("maintenance")' in backup_text,
    )
    failures += check(
        "applied restore CLI owns exclusive maintenance runtime lock",
        'with runtime_lock("maintenance")' in restore_text,
    )
    failures += check(
        "runtime lock finally block does not return while nested holds remain",
        'if int(current["count"]) > 0:\n                return' not in runtime_lock_text,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
