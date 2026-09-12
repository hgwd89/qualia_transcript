import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.local_backup import restore_backup
from services.runtime_lock import RuntimeLockError, runtime_lock


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate or restore a Qualia Transcript backup. "
            "Validation-only is safe while the app is running; applied restore is not."
        )
    )
    parser.add_argument("archive", help="Backup ZIP path")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually replace the local DB/uploads/outputs after validation",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required acknowledgement for destructive --apply restore",
    )
    parser.add_argument(
        "--allow-unvalidated-pre-restore",
        action="store_true",
        help=(
            "If the current DB is damaged and a verified pre-restore backup cannot be "
            "created, preserve a raw owner-only copy of that DB and continue restore. "
            "Requires --apply and --yes."
        ),
    )
    args = parser.parse_args()

    if args.apply and not args.yes:
        print("[FAIL] restore requires both --apply and --yes")
        return 2
    if args.allow_unvalidated_pre_restore and not (args.apply and args.yes):
        print("[FAIL] --allow-unvalidated-pre-restore requires --apply and --yes")
        return 2

    try:
        if args.apply:
            with runtime_lock("maintenance"):
                result = restore_backup(
                    args.archive,
                    apply=True,
                    allow_unvalidated_pre_restore=args.allow_unvalidated_pre_restore,
                )
        else:
            result = restore_backup(args.archive, apply=False)
    except RuntimeLockError as exc:
        print(f"[FAIL] applied restore refused while Qualia Transcript or maintenance is active: {exc}")
        return 3

    if not args.apply:
        print("[PASS] backup validation succeeded; no files were changed")
        print(f"[INFO] created_at_utc: {result['manifest'].get('created_at_utc')}")
        print(f"[INFO] files: {len(result['manifest'].get('files') or [])}")
        return 0

    print("[PASS] restore completed")
    if result.get("pre_restore_backup"):
        print(f"[PASS] pre-restore safety backup: {result['pre_restore_backup']}")
    if result.get("pre_restore_database_copy"):
        print(
            "[WARN] verified pre-restore backup could not be created; "
            f"damaged DB preserved at: {result['pre_restore_database_copy']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
