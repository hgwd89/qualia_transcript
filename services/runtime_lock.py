from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

import config


class RuntimeLockError(RuntimeError):
    """Raised when application/worker and maintenance lifecycles overlap."""


_STATE_GUARD = RLock()
_HELD: dict[str, dict[str, object]] = {}


def _default_lock_path() -> Path:
    configured = getattr(config, "RUNTIME_LOCK_PATH", None)
    if configured:
        return Path(configured).resolve()
    instance_dir = Path(
        getattr(config, "INSTANCE_DIR", Path(config.BASE_DIR) / "instance")
    )
    return (instance_dir / "runtime.lock").resolve()


def _role_mode(role: str) -> str:
    if role in {"app", "worker"}:
        return "shared"
    if role == "maintenance":
        return "exclusive"
    raise ValueError("runtime lock role must be 'app', 'worker', or 'maintenance'")


def _lock_file(handle, mode: str) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        lock_mode = (
            msvcrt.LK_NBRLCK if mode == "shared" else msvcrt.LK_NBLCK
        )
        try:
            msvcrt.locking(handle.fileno(), lock_mode, 1)
        except OSError as exc:
            raise RuntimeLockError(
                "Qualia runtime is busy: application/worker and maintenance operations cannot overlap"
            ) from exc
        return

    import fcntl

    flag = fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX
    try:
        fcntl.flock(handle.fileno(), flag | fcntl.LOCK_NB)
    except OSError as exc:
        raise RuntimeLockError(
            "Qualia runtime is busy: application/worker and maintenance operations cannot overlap"
        ) from exc


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def runtime_lock(role: str, lock_path: str | os.PathLike | None = None):
    """Hold a process-lifetime runtime lock for one application operation.

    App and durable-worker processes take a shared lock so they can coexist.
    Backup/applied-restore maintenance takes an exclusive lock, so it cannot run
    while either the web app or a detached worker can still write application
    state. Nested acquisition is allowed only when the requested lock mode matches
    the mode already held by this process; this lets restore call backup while
    retaining one exclusive maintenance boundary.
    """
    mode = _role_mode(role)
    path = Path(lock_path).resolve() if lock_path else _default_lock_path()
    key = str(path)

    with _STATE_GUARD:
        existing = _HELD.get(key)
        if existing is not None:
            existing_mode = str(existing["mode"])
            if existing_mode != mode:
                raise RuntimeLockError(
                    f"runtime lock already held in {existing_mode} mode; cannot acquire {mode} mode"
                )
            existing["count"] = int(existing["count"]) + 1
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                _lock_file(handle, mode)
            except Exception:
                handle.close()
                raise
            _HELD[key] = {
                "mode": mode,
                "count": 1,
                "handle": handle,
                "roles": {role},
            }

    try:
        yield path
    finally:
        with _STATE_GUARD:
            current = _HELD.get(key)
            if current is None:
                return
            current["count"] = int(current["count"]) - 1
            if int(current["count"]) > 0:
                return
            handle = current["handle"]
            _HELD.pop(key, None)
            _unlock_file(handle)
            handle.close()
