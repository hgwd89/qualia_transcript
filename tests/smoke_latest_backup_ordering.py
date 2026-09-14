import os
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def set_mtime_ns(path: Path, value: int) -> None:
    os.utime(path, ns=(value, value))


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    import services.local_backup as local_backup
    from services.readiness_validation import validate_latest_backup

    original_validate = local_backup.validate_backup
    try:
        with tempfile.TemporaryDirectory(prefix="qualia_latest_backup_ordering_") as tmp:
            root = Path(tmp)
            same_second = root / "same-second"
            same_second.mkdir()

            older = same_second / "qualia_backup_20260914T080000Z_z_old_aaaaaaaa.zip"
            newer = same_second / "qualia_backup_20260914T080000Z_a_new_bbbbbbbb.zip"
            older.write_bytes(b"older")
            newer.write_bytes(b"newer")
            set_mtime_ns(older, 1_700_000_000_000_000_000)
            set_mtime_ns(newer, 1_700_000_010_000_000_000)

            failures += check(
                "fixture reproduces legacy lexical-order inversion within one encoded second",
                sorted([older, newer])[-1] == older,
                f"lexical_latest={sorted([older, newer])[-1].name}",
            )

            selected: list[Path] = []

            def validate_same_second(path):
                candidate = Path(path)
                selected.append(candidate)
                if candidate == newer:
                    raise ValueError("forced newer backup corruption")
                return {"format": "qualia-transcript-backup"}

            local_backup.validate_backup = validate_same_second
            latest, error = validate_latest_backup(same_second)
            failures += check(
                "same-second readiness selects the later-published archive instead of label/UUID order",
                latest == newer and selected == [newer],
                f"latest={latest} selected={selected}",
            )
            failures += check(
                "corruption in the later-published same-second backup is not hidden by an older valid archive",
                error is not None and "forced newer backup corruption" in error,
                str(error or ""),
            )

            cross_second = root / "cross-second"
            cross_second.mkdir()
            earlier_timestamp = cross_second / "qualia_backup_20260914T080000Z_z_earlier_cccccccc.zip"
            later_timestamp = cross_second / "qualia_backup_20260914T080001Z_a_later_dddddddd.zip"
            earlier_timestamp.write_bytes(b"earlier timestamp")
            later_timestamp.write_bytes(b"later timestamp")
            # Deliberately make the older filename timestamp have the newer mtime.
            # Recency must still be driven by the encoded UTC second first.
            set_mtime_ns(earlier_timestamp, 1_700_000_020_000_000_000)
            set_mtime_ns(later_timestamp, 1_700_000_000_000_000_000)

            selected.clear()

            def validate_cross_second(path):
                selected.append(Path(path))
                return {"format": "qualia-transcript-backup"}

            local_backup.validate_backup = validate_cross_second
            latest, error = validate_latest_backup(cross_second)
            failures += check(
                "encoded UTC timestamp remains authoritative across different seconds",
                latest == later_timestamp and selected == [later_timestamp] and error is None,
                f"latest={latest} selected={selected} error={error}",
            )
    finally:
        local_backup.validate_backup = original_validate

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
