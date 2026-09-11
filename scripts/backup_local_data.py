import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from services.local_backup import create_backup, validate_backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a verified Qualia Transcript local backup archive."
    )
    parser.add_argument(
        "--destination",
        default=config.BACKUP_DIR,
        help="Directory for backup ZIP files (default: backups/)",
    )
    parser.add_argument("--label", default="manual", help="Short backup label")
    args = parser.parse_args()

    archive = create_backup(args.destination, label=args.label)
    manifest = validate_backup(archive)
    print(f"[PASS] backup created: {archive}")
    print(f"[PASS] files verified: {len(manifest.get('files') or [])}")
    print(f"[INFO] created_at_utc: {manifest.get('created_at_utc')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
