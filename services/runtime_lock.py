from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

import config


class RuntimeLockError(RuntimeError):
    """Raised when live-runtime access and maintenance lifecycles overlap."""


_STATE_GUARD = RLock()
_HELD: dict[str, dict[str, object]] = {}
_RUNTIME_SLOTS = 128


def _default_lock_path() -> Path:
    configured = getattr(config, "RUNTIME_LOCK_PATH", None)
    if configured:
        return Path(configured).resolve()
    instance_dir = Path(
        getattr(config, "INSTANCE_DIR", Path(config.BASE_DIR) / "instance")
    )
    return (instance_dir / "runtime.lock").resolve()


def _role_mode(role: str) -> str:
    if role in {"app", "worker", "reader"}:
        return "runtime"
    if role == "maintenance":
        return "maintenance"
    raise ValueError(
        "runtime lock role must be 'app', 'worker', 'reader', or 'maintenance'"
    )


def _ensure_lock_file_size(handle) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() < _RUNTIME_SLOTS:
        handle.write(b"\0" * (_RUNTIME_SLOTS - handle.tell()))
        handle.flush()


def _try_lock_range(handle, offset: int, length: int) -> bool:
    handle.seek(offset)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, length)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.lockf(
            handle.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
            length,
            offset,
            os.SEEK_SET,
        )
        return True
    except OSError:
        return False


def _unlock_range(handle, offset: int, length: int) -> None:
    handle.seek(offset)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, length)
        except OSError:
            pass
        return

    import fcntl

    try:
        fcntl.lockf(
            handle.fileno(),
            fcntl.LOCK_UN,
            length,
            offset,
            os.SEEK_SET,
        )
    except OSError:
        pass


def _acquire_range(handle, mode: str) -> tuple[int, int]:
    if mode == "maintenance":
        if _try_lock_range(handle, 0, _RUNTIME_SLOTS):
            return 0, _RUNTIME_SLOTS
        raise RuntimeLockError(
            "Qualia runtime is busy: stop all live runtime access before maintenance"
        )

    for offset in range(_RUNTIME_SLOTS):
        if _try_lock_range(handle, offset, 1):
            return offset, 1
    raise RuntimeLockError(
        "Qualia runtime has no free process lock slots or maintenance is active"
    )


@contextmanager
def runtime_lock(role: str, lock_path: str | os.PathLike | None = None):
    """Hold the runtime/maintenance exclusion lock for one process lifecycle.

    App, detached worker, and operational reader processes each reserve one
    byte-range slot, allowing normal live access to coexist. Backup and applied
    restore lock the complete slot range, so maintenance cannot start while any
    live reader/writer process remains and no live accessor can start while
    maintenance owns the range.

    Nested acquisition is allowed only for the same lock mode in the same process.
    This lets applied restore invoke a pre-restore backup while retaining one
    exclusive maintenance boundary and lets a reader path enter a write helper
    without dropping the shared runtime reservation.
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
                _ensure_lock_file_size(handle)
                offset, length = _acquire_range(handle, mode)
            except Exception:
                handle.close()
                raise
            _HELD[key] = {
                "mode": mode,
                "count": 1,
                "handle": handle,
                "offset": offset,
                "length": length,
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
            offset = int(current["offset"])
            length = int(current["length"])
            _HELD.pop(key, None)
            _unlock_range(handle, offset, length)
            handle.close()
