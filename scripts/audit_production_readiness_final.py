"""Final professional-readiness entry point including generated-file ownership.

This wrapper preserves the hardened database-wide/project-scoped readiness audits
and adds the cross-project GeneratedFile ownership checks that individual SQLite
foreign keys cannot express.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_production_readiness as base_readiness
import audit_production_readiness_project as project_readiness
import audit_production_readiness_v2 as readiness_v2
from services.generated_file_ownership import inspect_generated_file_ownership
from services.runtime_lock import RuntimeLockError, runtime_lock


def _append_generated_file_ownership(
    report: dict,
    db_path: Path,
    *,
    project_id: int | None,
) -> None:
    con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        ownership = inspect_generated_file_ownership(con, project_id=project_id)
    except Exception as exc:
        report.setdefault("blockers", []).append({
            "code": "generated_file_ownership_audit_error",
            "message": "Could not validate GeneratedFile project ownership",
            "context": {"error": f"{type(exc).__name__}: {exc}"},
        })
        return
    finally:
        con.close()

    report.setdefault("blockers", []).extend(ownership.blockers)
    report.setdefault("warnings", []).extend(ownership.warnings)
    info = report.setdefault("info", {})
    info["generated_file_ownership_checked_count"] = ownership.checked_count
    info["generated_file_ownership_blocker_count"] = len(ownership.blockers)
    info["generated_file_ownership_warning_count"] = len(ownership.warnings)


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def audit_final(
    db_path: Path,
    output_dir: Path,
    backup_dir: Path,
    upload_dir: Path,
    *,
    project_id: int | None = None,
) -> dict:
    if project_id is None:
        report = readiness_v2.audit(db_path, output_dir, backup_dir, upload_dir)
    else:
        report = project_readiness.audit_project(
            db_path,
            output_dir,
            backup_dir,
            int(project_id),
            upload_dir,
        )

    _append_generated_file_ownership(report, db_path, project_id=project_id)
    report["blockers"] = _dedupe(report.get("blockers", []))
    report["warnings"] = _dedupe(report.get("warnings", []))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Final hardened production-readiness audit")
    parser.add_argument("--project-id", type=int, help="Optional project-scoped business audit")
    parser.add_argument("--db", help="SQLite DB path; defaults to config.DATABASE_URI")
    parser.add_argument("--output-dir", help="outputs directory; defaults to config.OUTPUT_DIR")
    parser.add_argument("--backup-dir", help="backup directory; defaults to config.BACKUP_DIR")
    parser.add_argument("--upload-dir", help="uploads directory; defaults to config.UPLOAD_DIR")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    parser.add_argument("--strict", action="store_true", help="treat warnings as a failing exit status")
    args = parser.parse_args()

    db_path = base_readiness._resolve_db_path(args.db)
    output_dir = base_readiness._resolve_output_dir(args.output_dir)
    backup_dir = base_readiness._resolve_backup_dir(args.backup_dir)
    upload_dir = readiness_v2._resolve_upload_dir(args.upload_dir)

    try:
        with runtime_lock("reader"):
            try:
                report = audit_final(
                    db_path,
                    output_dir,
                    backup_dir,
                    upload_dir,
                    project_id=args.project_id,
                )
            except Exception as exc:
                report = {
                    "blockers": [{
                        "code": "audit_error",
                        "message": f"{type(exc).__name__}: {exc}",
                    }],
                    "warnings": [],
                    "info": {
                        "scope": "project" if args.project_id is not None else "database",
                        "project_id": args.project_id,
                    },
                }
    except RuntimeLockError as exc:
        report = {
            "blockers": [{
                "code": "maintenance_active",
                "message": "Final readiness audit refused while backup/restore maintenance is active",
                "context": {"error": str(exc)},
            }],
            "warnings": [],
            "info": {
                "scope": "project" if args.project_id is not None else "database",
                "project_id": args.project_id,
            },
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            base_readiness._print_report(report)
        return 3

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        if args.project_id is not None:
            print(f"Scope: project_id={args.project_id}")
        base_readiness._print_report(report)

    if report.get("blockers"):
        return 1
    if args.strict and report.get("warnings"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
