from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

import config


class RuntimeLockError(RuntimeError):
    """Raised when application and maintenance lifecycles overlap."""


_STATE_GUARD = RLock()
_HELD: dict[str, dict[str, object]] = {}


def _default_lock_path() -> Path:
    instance_dir = Path(
        getattr(config, "INSTANCE_DIR", Path(config.BASE_DIR) / "instance")
    )
    return (instance_dir / "runtime.lock").resolve()


def _lock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeLockError(
                "application/maintenance lock is already held; stop the running app or maintenance operation first"
            ) from exc
        return

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise RuntimeLockError(
            "application/maintenance lock is already held; stop the running app or maintenance operation first"
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
    """Hold the application/maintenance exclusion lock for one lifecycle.

    `role` is either ``app`` or ``maintenance``. Re-entry is allowed only for the
    same role in the same process. This lets applied restore call the normal
    backup service for its pre-restore safety copy while still preventing backup
    or restore from running inside a live application process.
    """
    if role not in {"app", "maintenance"}:
        raise ValueError("runtime lock role must be 'app' or 'maintenance'")

    path = Path(lock_path).resolve() if lock_path else _default_lock_path()
    key = str(path)
    acquired_new = False

    with _STATE_GUARD:
        existing = _HELD.get(key)
        if existing is not None:
            existing_role = str(existing["role"])
            if existing_role != role:
                raise RuntimeLockError(
                    f"runtime lock is already held by {existing_role}; cannot start {role}"
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
                _lock_file(handle)
            except Exception:
                handle.close()
                raise
            _HELD[key] = {"role": role, "count": 1, "handle": handle}
            acquired_new = True

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
