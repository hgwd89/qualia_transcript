from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.runtime_lock import RuntimeLockError, runtime_lock


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: run_with_runtime_reader.py <python-script> [args...]")
        return 2

    target = Path(sys.argv[1])
    if not target.is_absolute():
        target = (ROOT / target).resolve()
    try:
        target.relative_to(ROOT)
    except ValueError:
        print("refusing reader target outside repository")
        return 2
    if not target.is_file() or target.suffix.lower() != ".py":
        print(f"reader target is not a Python script: {target}")
        return 2

    command = [sys.executable, str(target), *sys.argv[2:]]
    try:
        with runtime_lock("reader"):
            completed = subprocess.run(command, cwd=str(ROOT), check=False)
    except RuntimeLockError as exc:
        print(f"refusing live-state read while maintenance is active: {exc}")
        return 3
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
