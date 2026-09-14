"""Hardened professional-readiness audit entry point.

Extends the base read-only audit with structural validation of registered Office
artifacts, immutable raw transcript snapshots, the newest local backup,
processing-job quiescence/scope integrity, and database-level foreign-key
consistency.
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

from services.formal_artifact_integrity import (
    FormalArtifactIntegrityError,
    verified_artifact_snapshot,
)
from services.readiness_validation import (
    load_raw_snapshot_tombstone_names,
    load_raw_text_snapshots,
    missing_raw_snapshot_transcription_ids,
    validate_generated_artifact,
    validate_latest_backup,
)
from services.runtime_lock import RuntimeLockError, runtime_lock
from services.storage_paths import open_managed_file_for_read


QUESTION_GUARD_TRIGGERS = {
    "trg_processing_jobs_question_insert",
    "trg_processing_jobs_question_update",
}
JOB_SCOPE_RULES = {
    "transcribe": (True, False),
    "map": (True, False),
    "analyze": (True, False),
    "analyze_semantic": (True, False),
    "analyze_question": (True, True),
    "analyze_cross": (False, True),
    "analyze_integrated": (False, False),
    "project_pipeline": (False, False),
}
JOB_SCOPE_TABLES = {
    "processing_jobs",
    "projects",
    "interviews",
    "interview_flows",
    "interview_flow_sections",
    "interview_flow_questions",
}
JOB_REPORT_LIMIT = 200
ACTIVE_JOB_REPORT_LIMIT = 100
PROJECT_JOB_SAMPLE_LIMIT = 5


def _issue(bucket: list[dict], code: str, message: str, **context) -> None:
    item = {"code": code, "message": message}
    if context:
        item["context"] = context
    bucket.append(item)


def _processing_job_scope_issues(con: sqlite3.Connection) -> list[dict]:
    """Return semantic durable-job scope violations from the real main tables."""
    rows = con.execute(
        """
        SELECT
            pj.id,
            pj.project_id,
            pj.interview_id,
            pj.question_id,
            pj.job_type,
            pj.status,
            i.id AS linked_interview_id,
            i.project_id AS interview_project_id,
            i.flow_id AS interview_flow_id,
            q.id AS linked_question_id,
            sec.flow_id AS question_flow_id,
            flow.project_id AS question_project_id
        FROM main.processing_jobs pj
        LEFT JOIN main.interviews i ON i.id=pj.interview_id
        LEFT JOIN main.interview_flow_questions q ON q.id=pj.question_id
        LEFT JOIN main.interview_flow_sections sec ON sec.id=q.section_id
        LEFT JOIN main.interview_flows flow ON flow.id=sec.flow_id
        ORDER BY pj.id
        """
    ).fetchall()

    invalid: list[dict] = []
    for row in rows:
        project_id = int(row["project_id"])
        interview_id = row["interview_id"]
        question_id = row["question_id"]
        job_type = str(row["job_type"] or "")
        reasons: list[str] = []

        rule = JOB_SCOPE_RULES.get(job_type)
        if rule is None:
            reasons.append("unsupported_job_type")
        else:
            interview_required, question_required = rule
            if interview_required and interview_id is None:
                reasons.append("interview_required")
            if not interview_required and interview_id is not None:
                reasons.append("interview_forbidden")
            if question_required and question_id is None:
                reasons.append("question_required")
            if not question_required and question_id is not None:
                reasons.append("question_forbidden")

        if interview_id is not None:
            if row["linked_interview_id"] is None:
                reasons.append("interview_missing")
            elif int(row["interview_project_id"]) != project_id:
                reasons.append("interview_cross_project")

        if question_id is not None and row["linked_question_id"] is not None:
            if row["question_project_id"] is None:
                reasons.append("question_flow_unresolvable")
            elif int(row["question_project_id"]) != project_id:
                reasons.append("question_cross_project")

        if (
            job_type == "analyze_question"
            and interview_id is not None
            and question_id is not None
            and row["linked_interview_id"] is not None
            and row["linked_question_id"] is not None
        ):
            interview_flow_id = row["interview_flow_id"]
            question_flow_id = row["question_flow_id"]
            if interview_flow_id is None:
                reasons.append("interview_flow_missing")
            elif question_flow_id is not None and int(question_flow_id) != int(interview_flow_id):
                reasons.append("question_wrong_interview_flow")

        if reasons:
            invalid.append({
                "id": int(row["id"]),
                "project_id": project_id,
                "interview_id": int(interview_id) if interview_id is not None else None,
                "question_id": int(question_id) if question_id is not None else None,
                "job_type": job_type,
                "status": row["status"],
                "reasons": sorted(set(reasons)),
            })
    return invalid


def _processing_job_project_summary(
    jobs: list[dict],
) -> tuple[dict[str, int], dict[str, list[dict]]]:
    """Retain exact scoped counts before capping a global diagnostic sample."""
    counts: dict[str, int] = {}
    samples: dict[str, list[dict]] = {}
    for job in jobs:
        key = str(int(job["project_id"]))
        counts[key] = counts.get(key, 0) + 1
        bucket = samples.setdefault(key, [])
        if len(bucket) < PROJECT_JOB_SAMPLE_LIMIT:
            bucket.append(dict(job))
    return counts, samples


def _job_issue_context(jobs: list[dict], report_limit: int) -> dict:
    project_counts, project_samples = _processing_job_project_summary(jobs)
    return {
        "jobs": jobs[:report_limit],
        "count": len(jobs),
        "project_counts": project_counts,
        "project_samples": project_samples,
    }


def _formal_artifact_hash_reason(row: sqlite3.Row, output_dir: Path) -> str | None:
    """Return why a registered formal artifact cannot pass the delivery byte gate."""
    try:
        params = json.loads(row["generation_params_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return "formal artifact generation metadata is invalid"
    if not isinstance(params, dict):
        return "formal artifact generation metadata is invalid"

    expected_sha256 = str(params.get("artifact_sha256") or "").strip().lower()
    if len(expected_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha256):
        return "formal artifact SHA-256 metadata is missing or invalid"

    opened = None
    verified = None
    try:
        opened = open_managed_file_for_read(output_dir, str(row["stored_path"] or ""))
        verified = verified_artifact_snapshot(opened, expected_sha256)
        opened = None
        return None
    except (FormalArtifactIntegrityError, OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if opened is not None:
            opened.close()
        if verified is not None:
            verified.close()


def audit(db_path: Path, output_dir: Path, backup_dir: Path) -> dict:
    report = base_audit(db_path, output_dir, backup_dir)
    blockers = report["blockers"]
    warnings = report["warnings"]
    info = report["info"]

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
        tables = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

        fk_rows = con.execute("PRAGMA foreign_key_check").fetchall()
        info["foreign_key_violation_count"] = len(fk_rows)
        if fk_rows:
            violations = []
            for row in fk_rows[:JOB_REPORT_LIMIT]:
                values = list(row)
                violations.append({
                    "table": values[0] if len(values) > 0 else None,
                    "rowid": values[1] if len(values) > 1 else None,
                    "parent": values[2] if len(values) > 2 else None,
                    "fk_index": values[3] if len(values) > 3 else None,
                })
            _issue(
                blockers,
                "sqlite_foreign_key_violations",
                "Existing SQLite rows violate declared foreign-key relationships",
                violations=violations,
                count=len(fk_rows),
            )

        if "processing_jobs" not in tables:
            _issue(
                blockers,
                "processing_jobs_table_missing",
                "Durable processing_jobs table is missing; start the upgraded app once before professional use",
            )
        else:
            job_columns = {
                str(row["name"])
                for row in con.execute("PRAGMA table_info(processing_jobs)").fetchall()
            }
            if "request_json" not in job_columns:
                _issue(
                    blockers,
                    "processing_job_request_json_column_missing",
                    "processing_jobs.request_json is missing; start the upgraded app once before professional use",
                )
            if "question_id" not in job_columns:
                _issue(
                    blockers,
                    "processing_job_question_column_missing",
                    "processing_jobs.question_id is missing; start the upgraded app once before professional use",
                )
            else:
                declared_fk = any(
                    str(row["table"]) == "interview_flow_questions"
                    and str(row["from"]) == "question_id"
                    and str(row["to"]) == "id"
                    for row in con.execute("PRAGMA foreign_key_list(processing_jobs)").fetchall()
                )
                trigger_names = {
                    str(row["name"])
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='processing_jobs'"
                    ).fetchall()
                }
                trigger_guard = QUESTION_GUARD_TRIGGERS.issubset(trigger_names)
                info["processing_job_question_guard"] = {
                    "declared_fk": declared_fk,
                    "trigger_guard": trigger_guard,
                }
                if not declared_fk and not trigger_guard:
                    _issue(
                        blockers,
                        "processing_job_question_guard_missing",
                        "Legacy processing_jobs.question_id has no FK or compatibility trigger guard; start the upgraded app once before professional use",
                    )

                orphan_rows = con.execute(
                    """
                    SELECT pj.id, pj.project_id, pj.interview_id, pj.question_id, pj.job_type, pj.status
                    FROM main.processing_jobs pj
                    LEFT JOIN main.interview_flow_questions q ON q.id=pj.question_id
                    WHERE pj.question_id IS NOT NULL AND q.id IS NULL
                    ORDER BY pj.id
                    """
                ).fetchall()
                orphan_jobs = [dict(row) for row in orphan_rows]
                info["processing_job_question_orphan_count"] = len(orphan_jobs)
                if orphan_jobs:
                    _issue(
                        blockers,
                        "processing_job_question_orphans",
                        "ProcessingJob rows reference missing interview-flow questions",
                        **_job_issue_context(orphan_jobs, JOB_REPORT_LIMIT),
                    )

                if JOB_SCOPE_TABLES.issubset(tables):
                    invalid_scope_jobs = _processing_job_scope_issues(con)
                    info["processing_job_scope_invalid_count"] = len(invalid_scope_jobs)
                    if invalid_scope_jobs:
                        _issue(
                            blockers,
                            "processing_job_scope_invalid",
                            "ProcessingJob rows violate durable job ownership or job-type scope rules",
                            **_job_issue_context(invalid_scope_jobs, JOB_REPORT_LIMIT),
                        )
                else:
                    info["processing_job_scope_invalid_count"] = 0

            active_rows = con.execute(
                """
                SELECT id, project_id, interview_id, job_type, status, progress_json, created_at, started_at
                FROM main.processing_jobs
                WHERE status IN ('pending','running')
                ORDER BY id
                """
            ).fetchall()
            active_jobs = [dict(row) for row in active_rows]
            info["active_processing_job_count"] = len(active_jobs)
            if active_jobs:
                _issue(
                    warnings,
                    "active_processing_jobs",
                    "Processing jobs are still pending/running; wait for a quiescent dataset before final delivery",
                    **_job_issue_context(active_jobs, ACTIVE_JOB_REPORT_LIMIT),
                )

        generated_columns = {
            str(row["name"])
            for row in con.execute("PRAGMA table_info(generated_files)").fetchall()
        }
        generation_params_select = (
            "generation_params_json"
            if "generation_params_json" in generated_columns
            else "NULL AS generation_params_json"
        )
        generated = con.execute(
            f"""
            SELECT id, file_type, file_format, stored_path, {generation_params_select}
            FROM generated_files
            ORDER BY id
            """
        ).fetchall()
        for row in generated:
            path = _safe_output_path(output_dir.resolve(), str(row["stored_path"] or ""))
            if path is None or not path.is_file() or path.stat().st_size <= 0:
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

            if str(row["file_type"] or "") == "approved_analysis":
                byte_reason = _formal_artifact_hash_reason(row, output_dir)
                if byte_reason:
                    _issue(
                        blockers,
                        "approved_analysis_artifact_integrity_invalid",
                        "Registered formal approved-analysis artifact would be rejected by the delivery byte-integrity gate",
                        generated_file_id=row["id"],
                        path=str(path),
                        reason=byte_reason,
                    )

        raw_by_transcription, invalid_raw = load_raw_text_snapshots(output_dir)
        tombstoned_snapshot_names = load_raw_snapshot_tombstone_names(con)
        info["raw_text_snapshot_count"] = sum(len(v) for v in raw_by_transcription.values())
        info["raw_snapshot_tombstone_count"] = len(tombstoned_snapshot_names)
        if invalid_raw:
            _issue(
                blockers,
                "raw_snapshot_invalid",
                "Raw transcript snapshot failed structural/hash validation",
                files=invalid_raw[:100],
                count=len(invalid_raw),
            )

        done_rows = con.execute(
            """
            SELECT tr.id, mf.interview_id, tr.started_at, tr.completed_at
            FROM transcriptions tr
            JOIN media_files mf ON mf.id=tr.media_file_id
            WHERE tr.status='done'
            ORDER BY tr.id
            """
        ).fetchall()
        missing = missing_raw_snapshot_transcription_ids(
            done_rows,
            raw_by_transcription,
            tombstoned_snapshot_names,
        )
        if missing:
            _issue(
                warnings,
                "done_transcription_without_raw_snapshot",
                "Completed transcriptions have no immutable raw text snapshot from their current generation",
                transcription_ids=missing[:JOB_REPORT_LIMIT],
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
        with runtime_lock("reader"):
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
    except RuntimeLockError as exc:
        report = {
            "blockers": [{
                "code": "maintenance_active",
                "message": "Readiness audit refused while backup/restore maintenance is active",
                "context": {"error": str(exc)},
            }],
            "warnings": [],
            "info": {},
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            _print_report(report)
        return 3

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
