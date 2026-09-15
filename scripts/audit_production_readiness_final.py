"""Final professional-readiness entry point including ownership/currentness gates.

The existing v2/project readiness audits own the SQLite read transaction and the
final PRAGMA data_version change-detection window. This wrapper injects extra
cross-table acceptance checks into that same transaction instead of opening a
second connection after the hardened audit has completed.
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
from services.mapping_input_readiness_sqlite import inspect_mapping_input_currentness
from services.ordinary_artifact_provenance_sqlite import (
    ORDINARY_ARTIFACT_TYPES,
    ordinary_artifact_currentness_sqlite,
)
from services.runtime_lock import RuntimeLockError, runtime_lock


def _append_generated_file_ownership(
    report: dict,
    con,
    *,
    project_id: int | None,
) -> None:
    """Append ownership findings from the caller-owned readiness snapshot."""
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


def _append_mapping_input_currentness(
    report: dict,
    con,
    *,
    project_id: int | None,
) -> None:
    """Append mapping-input findings from the same caller-owned DB snapshot."""
    try:
        mapping = inspect_mapping_input_currentness(con, project_id=project_id)
    except Exception as exc:
        report.setdefault("blockers", []).append({
            "code": "mapping_input_currentness_audit_error",
            "message": "Could not validate mapping input currentness",
            "context": {"error": f"{type(exc).__name__}: {exc}"},
        })
        return

    report.setdefault("blockers", []).extend(mapping.blockers)
    report.setdefault("warnings", []).extend(mapping.warnings)
    info = report.setdefault("info", {})
    info["mapping_input_currentness"] = {
        "checked": mapping.checked_count,
        "current": mapping.current_count,
        "invalid": mapping.invalid_count,
    }


def _append_ordinary_artifact_currentness(
    report: dict,
    con,
    *,
    project_id: int | None,
) -> None:
    """Classify ordinary export source provenance inside the audited DB snapshot.

    Historical stale outputs are retained by design, so they are warnings rather
    than blockers. The authoritative delivery boundary rejects an individual
    provenance-backed stale artifact with HTTP 409 when download is attempted.
    """
    placeholders = ",".join("?" for _ in ORDINARY_ARTIFACT_TYPES)
    params: list[object] = list(sorted(ORDINARY_ARTIFACT_TYPES))
    where = f"file_type IN ({placeholders})"
    if project_id is not None:
        where += " AND project_id=?"
        params.append(int(project_id))

    try:
        rows = con.execute(
            f"""
            SELECT id, project_id, interview_id, file_type, generation_params_json
            FROM generated_files
            WHERE {where}
            ORDER BY id
            """,
            tuple(params),
        ).fetchall()
    except Exception as exc:
        report.setdefault("blockers", []).append({
            "code": "ordinary_artifact_currentness_audit_error",
            "message": "Could not enumerate ordinary generated artifacts for source-currentness validation",
            "context": {"error": f"{type(exc).__name__}: {exc}"},
        })
        return

    current = 0
    unproven: list[dict] = []
    stale: list[dict] = []
    for row in rows:
        status = ordinary_artifact_currentness_sqlite(
            con,
            file_type=str(row["file_type"] or ""),
            project_id=int(row["project_id"]),
            interview_id=(int(row["interview_id"]) if row["interview_id"] is not None else None),
            generation_params_json=row["generation_params_json"],
        )
        item = {
            "generated_file_id": int(row["id"]),
            "project_id": int(row["project_id"]),
            "file_type": str(row["file_type"] or ""),
            "reason": status.reason,
        }
        if status.current:
            current += 1
        elif status.provenance_present:
            stale.append(item)
        else:
            unproven.append(item)

    info = report.setdefault("info", {})
    info["ordinary_artifact_source_currentness"] = {
        "checked": len(rows),
        "current": current,
        "stale": len(stale),
        "legacy_unproven": len(unproven),
    }
    if stale:
        report.setdefault("warnings", []).append({
            "code": "ordinary_artifact_source_stale",
            "message": "Historical provenance-backed ordinary generated artifacts no longer match current canonical source data and are not downloadable as current outputs",
            "context": {
                "files": stale[:100],
                "count": len(stale),
            },
        })
    if unproven:
        report.setdefault("warnings", []).append({
            "code": "ordinary_artifact_source_provenance_unproven",
            "message": "Legacy ordinary generated artifacts have no source provenance and cannot be proven current",
            "context": {
                "files": unproven[:100],
                "count": len(unproven),
            },
        })


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
    """Run hardened readiness plus cross-table gates in one SQLite snapshot."""
    original_runner = readiness_v2._run_base_audit_on_snapshot

    def run_base_with_extra_gates(con, inner_db_path, inner_output_dir, inner_backup_dir):
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
        _append_mapping_input_currentness(
            report,
            con,
            project_id=project_id,
        )
        _append_ordinary_artifact_currentness(
            report,
            con,
            project_id=project_id,
        )
        return report

    readiness_v2._run_base_audit_on_snapshot = run_base_with_extra_gates
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
