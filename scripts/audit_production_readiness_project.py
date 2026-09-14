"""Project-scoped professional-readiness audit.

The source SQLite database is always opened read-only. Project-owned application
rows are scoped through TEMP VIEWs that shadow the corresponding main tables only
for the lifetime of each audit connection. Database-level integrity/FK checks and
backup validation remain global because they describe the safety of the shared
SQLite/recovery set rather than one project's business rows.
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
import audit_production_readiness_v2 as readiness_v2
from services.runtime_lock import RuntimeLockError, runtime_lock


_ORIGINAL_SQLITE = sqlite3


def _install_project_views(con: sqlite3.Connection, project_id: int) -> None:
    """Shadow project-owned tables with read-only TEMP VIEWs for one project."""
    pid = int(project_id)
    con.executescript(
        f"""
        CREATE TEMP VIEW projects AS
            SELECT * FROM main.projects WHERE id={pid};

        CREATE TEMP VIEW participants AS
            SELECT * FROM main.participants WHERE project_id={pid};

        CREATE TEMP VIEW interview_flows AS
            SELECT * FROM main.interview_flows WHERE project_id={pid};

        CREATE TEMP VIEW interview_flow_sections AS
            SELECT sec.*
            FROM main.interview_flow_sections sec
            JOIN main.interview_flows f ON f.id=sec.flow_id
            WHERE f.project_id={pid};

        CREATE TEMP VIEW interview_flow_questions AS
            SELECT q.*
            FROM main.interview_flow_questions q
            JOIN main.interview_flow_sections sec ON sec.id=q.section_id
            JOIN main.interview_flows f ON f.id=sec.flow_id
            WHERE f.project_id={pid};

        CREATE TEMP VIEW interviews AS
            SELECT * FROM main.interviews WHERE project_id={pid};

        CREATE TEMP VIEW media_files AS
            SELECT mf.*
            FROM main.media_files mf
            JOIN main.interviews i ON i.id=mf.interview_id
            WHERE i.project_id={pid};

        CREATE TEMP VIEW transcriptions AS
            SELECT tr.*
            FROM main.transcriptions tr
            JOIN main.media_files mf ON mf.id=tr.media_file_id
            JOIN main.interviews i ON i.id=mf.interview_id
            WHERE i.project_id={pid};

        CREATE TEMP VIEW segments AS
            SELECT s.*
            FROM main.segments s
            JOIN main.interviews i ON i.id=s.interview_id
            WHERE i.project_id={pid};

        CREATE TEMP VIEW utterance_mappings AS
            SELECT um.*
            FROM main.utterance_mappings um
            JOIN main.segments s ON s.id=um.segment_id
            JOIN main.interviews i ON i.id=s.interview_id
            WHERE i.project_id={pid};

        CREATE TEMP VIEW speaker_assignments AS
            SELECT sa.*
            FROM main.speaker_assignments sa
            JOIN main.interviews i ON i.id=sa.interview_id
            WHERE i.project_id={pid};

        CREATE TEMP VIEW ai_analyses AS
            SELECT * FROM main.ai_analyses WHERE project_id={pid};

        CREATE TEMP VIEW generated_files AS
            SELECT * FROM main.generated_files WHERE project_id={pid};
        """
    )


class _ScopedSQLite:
    """Minimal sqlite3 proxy used only by the two readiness modules."""

    Row = _ORIGINAL_SQLITE.Row

    def __init__(self, project_id: int):
        self.project_id = int(project_id)

    def connect(self, *args, **kwargs):
        con = _ORIGINAL_SQLITE.connect(*args, **kwargs)
        _install_project_views(con, self.project_id)
        return con


def _project_exists(db_path: Path, project_id: int) -> bool:
    con = _ORIGINAL_SQLITE.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT 1 FROM projects WHERE id=?",
            (int(project_id),),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _filter_job_issue_list(items: list[dict], code: str, project_id: int) -> list[dict]:
    result: list[dict] = []
    project_key = str(int(project_id))
    for item in items:
        if item.get("code") != code:
            result.append(item)
            continue
        context = dict(item.get("context") or {})

        # Semantic job-scope diagnostics retain exact per-project counts and a
        # bounded project sample before the global diagnostic list is truncated.
        # Prefer that metadata so project readiness cannot miss a project's rows
        # merely because another project's first 200 rows filled the global sample.
        project_counts = context.get("project_counts")
        project_samples = context.get("project_samples")
        if isinstance(project_counts, dict):
            count = int(project_counts.get(project_key) or 0)
            if count <= 0:
                continue
            samples_by_project = project_samples if isinstance(project_samples, dict) else {}
            jobs = [
                dict(job)
                for job in (samples_by_project.get(project_key) or [])
            ]
            context["jobs"] = jobs
            context["count"] = count
            context.pop("project_counts", None)
            context.pop("project_samples", None)
            updated = dict(item)
            updated["context"] = context
            result.append(updated)
            continue

        jobs = [
            dict(job) for job in (context.get("jobs") or [])
            if int(job.get("project_id") or -1) == int(project_id)
        ]
        if not jobs:
            continue
        context["jobs"] = jobs
        context["count"] = len(jobs)
        updated = dict(item)
        updated["context"] = context
        result.append(updated)
    return result


def _count_job_issue(items: list[dict], code: str) -> int:
    return sum(
        int((item.get("context") or {}).get("count") or 0)
        for item in items
        if item.get("code") == code
    )


def audit_project(
    db_path: Path,
    output_dir: Path,
    backup_dir: Path,
    project_id: int,
    upload_dir: Path | None = None,
) -> dict:
    project_id = int(project_id)
    if project_id <= 0:
        return {
            "blockers": [{
                "code": "project_not_found",
                "message": "project_id must be a positive existing project ID",
                "context": {"project_id": project_id},
            }],
            "warnings": [],
            "info": {"project_id": project_id, "scope": "project"},
        }
    if not _project_exists(db_path, project_id):
        return {
            "blockers": [{
                "code": "project_not_found",
                "message": "Requested project does not exist in the selected database",
                "context": {"project_id": project_id},
            }],
            "warnings": [],
            "info": {"project_id": project_id, "scope": "project"},
        }

    scoped_sqlite = _ScopedSQLite(project_id)
    original_base_sqlite = base_readiness.sqlite3
    original_v2_sqlite = readiness_v2.sqlite3
    base_readiness.sqlite3 = scoped_sqlite
    readiness_v2.sqlite3 = scoped_sqlite
    try:
        report = readiness_v2.audit(db_path, output_dir, backup_dir, upload_dir)
    finally:
        base_readiness.sqlite3 = original_base_sqlite
        readiness_v2.sqlite3 = original_v2_sqlite

    report["blockers"] = _filter_job_issue_list(
        report.get("blockers", []),
        "processing_job_question_orphans",
        project_id,
    )
    report["blockers"] = _filter_job_issue_list(
        report.get("blockers", []),
        "processing_job_scope_invalid",
        project_id,
    )
    report["warnings"] = _filter_job_issue_list(
        report.get("warnings", []),
        "active_processing_jobs",
        project_id,
    )

    info = report.setdefault("info", {})
    info["scope"] = "project"
    info["project_id"] = project_id
    info["processing_job_question_orphan_count"] = _count_job_issue(
        report["blockers"],
        "processing_job_question_orphans",
    )
    info["processing_job_scope_invalid_count"] = _count_job_issue(
        report["blockers"],
        "processing_job_scope_invalid",
    )
    info["active_processing_job_count"] = _count_job_issue(
        report["warnings"],
        "active_processing_jobs",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Project-scoped hardened production-readiness audit")
    parser.add_argument("--project-id", type=int, required=True, help="Project ID whose business rows should be audited")
    parser.add_argument("--db", help="SQLite DB path; defaults to config.DATABASE_URI")
    parser.add_argument("--output-dir", help="outputs directory; defaults to config.OUTPUT_DIR")
    parser.add_argument("--backup-dir", help="backup directory; defaults to config.BACKUP_DIR")
    parser.add_argument("--upload-dir", help="uploads directory; defaults to config.UPLOAD_DIR")
    parser.add_argument("--json", action="store_true", help="print JSON report")
    parser.add_argument("--strict", action="store_true", help="treat warnings as a failing exit status")
    args = parser.parse_args()

    try:
        with runtime_lock("reader"):
            try:
                report = audit_project(
                    base_readiness._resolve_db_path(args.db),
                    base_readiness._resolve_output_dir(args.output_dir),
                    base_readiness._resolve_backup_dir(args.backup_dir),
                    args.project_id,
                    readiness_v2._resolve_upload_dir(args.upload_dir),
                )
            except Exception as exc:
                report = {
                    "blockers": [{"code": "audit_error", "message": f"{type(exc).__name__}: {exc}"}],
                    "warnings": [],
                    "info": {"scope": "project", "project_id": args.project_id},
                }
    except RuntimeLockError as exc:
        report = {
            "blockers": [{
                "code": "maintenance_active",
                "message": "Project readiness audit refused while backup/restore maintenance is active",
                "context": {"error": str(exc)},
            }],
            "warnings": [],
            "info": {"scope": "project", "project_id": args.project_id},
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print(f"Scope: project_id={args.project_id}")
            base_readiness._print_report(report)
        return 3

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Scope: project_id={args.project_id}")
        base_readiness._print_report(report)

    if report["blockers"]:
        return 1
    if args.strict and report["warnings"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
