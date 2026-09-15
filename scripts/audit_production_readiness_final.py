"""Final professional-readiness entry point for hardened deliverable validation.

The existing v2/project readiness audits own the SQLite read transaction and the
final PRAGMA data_version change-detection window. This wrapper injects composite
GeneratedFile ownership and ordinary-deliverable source-provenance checks into
that same transaction instead of opening a second connection after the hardened
audit has completed.
"""
from __future__ import annotations

import argparse
import json
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
from services.generated_file_source_provenance import (
    PROVENANCE_KEY as GENERATED_SOURCE_PROVENANCE_KEY,
    SUPPORTED_FILE_TYPES as SOURCE_BOUND_FILE_TYPES,
)
from services.generated_file_source_provenance_sqlite import (
    validate_generated_file_source_provenance,
)
from services.runtime_lock import RuntimeLockError, runtime_lock


def _append_generated_file_ownership(
    report: dict,
    con,
    *,
    project_id: int | None,
) -> None:
    try:
        ownership = inspect_generated_file_ownership(con, project_id=project_id)
    except Exception as exc:
        report.setdefault("blockers", []).append({
            "code": "generated_file_ownership_audit_error",
            "message": "Could not validate GeneratedFile project ownership",
            "context": {"error": f"{type(exc).__name__}: {exc}"},
        })
        return

    report.setdefault("blockers", []).extend(ownership.blockers)
    report.setdefault("warnings", []).extend(ownership.warnings)
    info = report.setdefault("info", {})
    info["generated_file_ownership_checked_count"] = ownership.checked_count
    info["generated_file_ownership_blocker_count"] = len(ownership.blockers)
    info["generated_file_ownership_warning_count"] = len(ownership.warnings)


def _append_generated_file_source_currentness(
    report: dict,
    con,
    *,
    project_id: int | None,
) -> None:
    """Validate ordinary deliverable source provenance on the readiness snapshot."""
    columns = {
        str(row["name"])
        for row in con.execute("PRAGMA table_info(main.generated_files)").fetchall()
    }
    required = {"id", "project_id", "interview_id", "file_type"}
    if not required.issubset(columns):
        report.setdefault("blockers", []).append({
            "code": "generated_file_source_provenance_schema_missing",
            "message": "GeneratedFile ownership/source columns are missing; upgrade before professional delivery",
            "context": {"missing_columns": sorted(required - columns)},
        })
        return

    if "generation_params_json" not in columns:
        report.setdefault("warnings", []).append({
            "code": "generated_file_source_provenance_unproven",
            "message": "Legacy generated_files schema has no generation_params_json; ordinary deliverable source currentness is unproven",
        })
        return

    params = ()
    where = ""
    if project_id is not None:
        where = "AND project_id=?"
        params = (int(project_id),)
    rows = con.execute(
        f"""
        SELECT id, project_id, interview_id, file_type, generation_params_json
        FROM main.generated_files
        WHERE file_type IN ('verbatim','formatted_sheet','analysis')
        {where}
        ORDER BY id
        """,
        params,
    ).fetchall()

    counts = {"current": 0, "unproven": 0, "invalid": 0}
    blockers = report.setdefault("blockers", [])
    warnings = report.setdefault("warnings", [])
    for row in rows:
        generated_file_id = int(row["id"])
        file_type = str(row["file_type"] or "")
        raw = row["generation_params_json"]
        if not raw:
            counts["unproven"] += 1
            warnings.append({
                "code": "generated_file_source_provenance_unproven",
                "message": "Legacy ordinary deliverable has no generation-time source provenance",
                "context": {
                    "generated_file_id": generated_file_id,
                    "file_type": file_type,
                },
            })
            continue
        try:
            metadata = json.loads(raw)
        except Exception as exc:
            counts["invalid"] += 1
            blockers.append({
                "code": "generated_file_source_provenance_invalid",
                "message": "Ordinary deliverable generation metadata is invalid",
                "context": {
                    "generated_file_id": generated_file_id,
                    "file_type": file_type,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            })
            continue
        if not isinstance(metadata, dict):
            counts["invalid"] += 1
            blockers.append({
                "code": "generated_file_source_provenance_invalid",
                "message": "Ordinary deliverable generation metadata is not a JSON object",
                "context": {
                    "generated_file_id": generated_file_id,
                    "file_type": file_type,
                },
            })
            continue
        expected = metadata.get(GENERATED_SOURCE_PROVENANCE_KEY)
        if expected is None:
            counts["unproven"] += 1
            warnings.append({
                "code": "generated_file_source_provenance_unproven",
                "message": "Legacy ordinary deliverable has no generation-time source provenance",
                "context": {
                    "generated_file_id": generated_file_id,
                    "file_type": file_type,
                },
            })
            continue
        raw_project_id = row["project_id"]
        if raw_project_id is None or file_type not in SOURCE_BOUND_FILE_TYPES:
            counts["invalid"] += 1
            blockers.append({
                "code": "generated_file_source_provenance_invalid",
                "message": "Ordinary deliverable source scope cannot be resolved",
                "context": {
                    "generated_file_id": generated_file_id,
                    "file_type": file_type,
                    "project_id": raw_project_id,
                },
            })
            continue
        current, reason = validate_generated_file_source_provenance(
            con,
            expected,
            file_type=file_type,
            project_id=int(raw_project_id),
            interview_id=(
                int(row["interview_id"])
                if row["interview_id"] is not None
                else None
            ),
        )
        if current:
            counts["current"] += 1
        else:
            counts["invalid"] += 1
            blockers.append({
                "code": "generated_file_source_provenance_invalid",
                "message": "Ordinary deliverable no longer matches the canonical source state used to generate it",
                "context": {
                    "generated_file_id": generated_file_id,
                    "file_type": file_type,
                    "reason": reason,
                },
            })

    info = report.setdefault("info", {})
    info["generated_file_source_provenance"] = counts


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
    """Run hardened readiness and derived-deliverable checks in one DB snapshot."""
    original_runner = readiness_v2._run_base_audit_on_snapshot

    def run_base_with_final_checks(con, inner_db_path, inner_output_dir, inner_backup_dir):
        report = original_runner(
            con,
            inner_db_path,
            inner_output_dir,
            inner_backup_dir,
        )
        _append_generated_file_ownership(
            report,
            con,
            project_id=project_id,
        )
        _append_generated_file_source_currentness(
            report,
            con,
            project_id=project_id,
        )
        return report

    readiness_v2._run_base_audit_on_snapshot = run_base_with_final_checks
    try:
        if project_id is None:
            report = readiness_v2.audit(
                db_path,
                output_dir,
                backup_dir,
                upload_dir,
            )
        else:
            report = project_readiness.audit_project(
                db_path,
                output_dir,
                backup_dir,
                int(project_id),
                upload_dir,
            )
    finally:
        readiness_v2._run_base_audit_on_snapshot = original_runner

    report["blockers"] = _dedupe(report.get("blockers", []))
    report["warnings"] = _dedupe(report.get("warnings", []))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Final hardened production-readiness audit"
    )
    parser.add_argument(
        "--project-id",
        type=int,
        help="Optional project-scoped business audit",
    )
    parser.add_argument("--db", help="SQLite DB path; defaults to config.DATABASE_URI")
    parser.add_argument("--output-dir", help="outputs directory; defaults to config.OUTPUT_DIR")
    parser.add_argument("--backup-dir", help="backup directory; defaults to config.BACKUP_DIR")
    parser.add_argument("--upload-dir", help="uploads directory; defaults to config.UPLOAD_DIR")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as a failing exit status",
    )
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
