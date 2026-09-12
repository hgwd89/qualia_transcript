import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from services.local_backup import create_backup


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

    # create_backup performs full manifest/hash/SQLite validation before returning.
    archive = create_backup(args.destination, label=args.label)
    print(f"[PASS] backup created and verified: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
