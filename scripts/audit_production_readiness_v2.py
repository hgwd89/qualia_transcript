"""Hardened professional-readiness audit entry point.

Extends the base read-only audit with structural validation of registered Office
artifacts, immutable raw transcript snapshots, and the newest local backup.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from audit_production_readiness import (
    _print_report,
    _resolve_backup_dir,
    _resolve_db_path,
    _resolve_output_dir,
    _safe_output_path,
    audit as base_audit,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.readiness_validation import (
    load_raw_text_snapshots,
    validate_generated_artifact,
    validate_latest_backup,
)


def _issue(bucket: list[dict], code: str, message: str, **context) -> None:
    item = {"code": code, "message": message}
    if context:
        item["context"] = context
    bucket.append(item)


def audit(db_path: Path, output_dir: Path, backup_dir: Path) -> dict:
    report = base_audit(db_path, output_dir, backup_dir)
    blockers = report["blockers"]
    warnings = report["warnings"]
    info = report["info"]

    # Remove base checks that are deliberately superseded by stronger validation.
    warnings[:] = [
        item for item in warnings
        if item.get("code") not in {"done_transcription_without_raw_snapshot", "no_backup_archive"}
    ]
    blockers[:] = [
        item for item in blockers
        if item.get("code") != "malformed_raw_transcript_snapshot"
    ]

    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        generated = con.execute(
            "SELECT id, file_format, stored_path FROM generated_files ORDER BY id"
        ).fetchall()
        for row in generated:
            path = _safe_output_path(output_dir.resolve(), str(row["stored_path"] or ""))
            if path is None or not path.is_file() or path.stat().st_size <= 0:
                # Base audit already reports unsafe/missing/empty artifacts.
                continue
            reason = validate_generated_artifact(path, str(row["file_format"] or ""))
            if reason:
                _issue(
                    blockers,
                    "generated_file_invalid",
                    "Registered generated artifact is structurally invalid",
                    generated_file_id=row["id"],
                    path=str(path),
                    reason=reason,
                )

        raw_by_transcription, invalid_raw = load_raw_text_snapshots(output_dir)
        info["raw_text_snapshot_count"] = sum(len(v) for v in raw_by_transcription.values())
        if invalid_raw:
            _issue(
                blockers,
                "raw_snapshot_invalid",
                "Raw transcript snapshot failed structural/hash validation",
                files=invalid_raw[:100],
                count=len(invalid_raw),
            )

        done_rows = con.execute(
            "SELECT id FROM transcriptions WHERE status='done' ORDER BY id"
        ).fetchall()
        missing = [
            int(row["id"]) for row in done_rows
            if int(row["id"]) not in raw_by_transcription
        ]
        if missing:
            _issue(
                warnings,
                "done_transcription_without_raw_snapshot",
                "Completed transcriptions have no immutable raw text snapshot",
                transcription_ids=missing[:200],
                count=len(missing),
            )
    finally:
        con.close()

    latest_backup, backup_error = validate_latest_backup(backup_dir)
    info["latest_validated_backup"] = str(latest_backup) if latest_backup else None
    if latest_backup is None:
        _issue(warnings, "no_backup_archive", "No local backup archive exists yet")
    elif backup_error:
        _issue(
            blockers,
            "latest_backup_invalid",
            "Newest local backup failed full manifest/hash/SQLite validation",
            path=str(latest_backup),
            error=backup_error,
        )

    # De-duplicate identical codes/context caused by base + hardened validation.
    def dedupe(items: list[dict]) -> list[dict]:
        seen = set()
        result = []
        for item in items:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    report["blockers"] = dedupe(blockers)
    report["warnings"] = dedupe(warnings)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Hardened read-only production readiness audit")
    parser.add_argument("--db", help="SQLite DB path; defaults to config.DATABASE_URI")
    parser.add_argument("--output-dir", help="outputs directory; defaults to config.OUTPUT_DIR")
    parser.add_argument("--backup-dir", help="backup directory; defaults to config.BACKUP_DIR")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    parser.add_argument("--strict", action="store_true", help="treat warnings as a failing exit status")
    args = parser.parse_args()

    try:
        report = audit(
            _resolve_db_path(args.db),
            _resolve_output_dir(args.output_dir),
            _resolve_backup_dir(args.backup_dir),
        )
    except Exception as exc:
        report = {
            "blockers": [{"code": "audit_error", "message": f"{type(exc).__name__}: {exc}"}],
            "warnings": [],
            "info": {},
        }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        _print_report(report)

    if report["blockers"]:
        return 1
    if args.strict and report["warnings"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
