import hashlib
import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path


def check(name: str, ok: bool, detail: str = "") -> int:
    status = "PASS" if ok else "FAIL"
    suffix = f": {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    return 0 if ok else 1


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_project_audit(repo_root: Path):
    scripts = repo_root / "scripts"
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    path = scripts / "audit_production_readiness_project.py"
    spec = importlib.util.spec_from_file_location("audit_production_readiness_project_smoke", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def create_fixture(db_path: Path) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE participants (id INTEGER PRIMARY KEY, project_id INTEGER);
            CREATE TABLE interview_flows (id INTEGER PRIMARY KEY, project_id INTEGER);
            CREATE TABLE interview_flow_sections (id INTEGER PRIMARY KEY, flow_id INTEGER);
            CREATE TABLE interview_flow_questions (id INTEGER PRIMARY KEY, section_id INTEGER);
            CREATE TABLE interviews (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                participant_id INTEGER,
                flow_id INTEGER,
                status TEXT
            );
            CREATE TABLE media_files (id INTEGER PRIMARY KEY, interview_id INTEGER);
            CREATE TABLE transcriptions (id INTEGER PRIMARY KEY, media_file_id INTEGER, status TEXT);
            CREATE TABLE segments (
                id INTEGER PRIMARY KEY,
                interview_id INTEGER,
                participant_id INTEGER,
                speaker_role TEXT,
                text TEXT
            );
            CREATE TABLE utterance_mappings (
                id INTEGER PRIMARY KEY,
                segment_id INTEGER,
                question_id INTEGER,
                is_unclassified INTEGER
            );
            CREATE TABLE speaker_assignments (
                id INTEGER PRIMARY KEY,
                interview_id INTEGER,
                participant_id INTEGER
            );
            CREATE TABLE ai_analyses (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                interview_id INTEGER,
                analysis_type TEXT,
                content_json TEXT,
                review_status TEXT
            );
            CREATE TABLE generated_files (
                id INTEGER PRIMARY KEY,
                project_id INTEGER,
                interview_id INTEGER,
                file_type TEXT,
                file_format TEXT,
                stored_path TEXT
            );
            CREATE TABLE processing_jobs (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                interview_id INTEGER,
                question_id INTEGER REFERENCES interview_flow_questions(id),
                job_type TEXT NOT NULL,
                status TEXT NOT NULL,
                progress_json TEXT,
                created_at DATETIME,
                started_at DATETIME
            );
            """
        )

        con.execute("INSERT INTO projects VALUES (1, 'Delivery Project')")
        con.execute("INSERT INTO projects VALUES (2, 'Unrelated Draft')")
        con.execute("INSERT INTO participants VALUES (1, 1)")
        con.execute("INSERT INTO participants VALUES (2, 2)")

        con.execute("INSERT INTO interview_flows VALUES (1, 1)")
        con.execute("INSERT INTO interview_flows VALUES (2, 2)")
        con.execute("INSERT INTO interview_flows VALUES (3, 1)")
        con.execute("INSERT INTO interview_flow_sections VALUES (1, 1)")
        con.execute("INSERT INTO interview_flow_sections VALUES (2, 2)")
        con.execute("INSERT INTO interview_flow_sections VALUES (3, 3)")
        con.execute("INSERT INTO interview_flow_questions VALUES (1, 1)")
        con.execute("INSERT INTO interview_flow_questions VALUES (2, 2)")
        con.execute("INSERT INTO interview_flow_questions VALUES (3, 3)")

        con.execute("INSERT INTO interviews VALUES (1, 1, 1, 1, 'transcribed')")
        # Keep project 2's interview flow unset so the existing mapping-flow
        # isolation regression remains meaningful.
        con.execute("INSERT INTO interviews VALUES (2, 2, 2, NULL, 'pending')")
        con.execute("INSERT INTO segments VALUES (2, 2, 2, 'unknown', 'draft response')")
        con.execute("INSERT INTO utterance_mappings VALUES (2, 2, 2, 0)")
        con.execute(
            "INSERT INTO generated_files VALUES (2, 2, 2, 'analysis', 'xlsx', '2/missing.xlsx')"
        )

        # Valid active row owned by project 2; project 1 readiness must not see it.
        con.execute(
            "INSERT INTO processing_jobs(id, project_id, interview_id, question_id, job_type, status, progress_json) "
            "VALUES (2, 2, 2, NULL, 'analyze', 'running', '{\"stage\":\"analyzing\"}')"
        )
        # All referenced IDs below exist, so these are semantic scope violations,
        # not ordinary FK/orphan failures.
        con.execute(
            "INSERT INTO processing_jobs(id, project_id, interview_id, question_id, job_type, status) "
            "VALUES (3, 1, 1, 3, 'analyze_question', 'succeeded')"
        )
        con.execute(
            "INSERT INTO processing_jobs(id, project_id, interview_id, question_id, job_type, status) "
            "VALUES (4, 1, 2, NULL, 'map', 'succeeded')"
        )
        con.execute(
            "INSERT INTO processing_jobs(id, project_id, interview_id, question_id, job_type, status) "
            "VALUES (5, 2, NULL, 1, 'analyze_cross', 'succeeded')"
        )
        con.commit()
    finally:
        con.close()


def codes(items: list[dict]) -> set[str]:
    return {str(item.get("code")) for item in items}


def issue_jobs(items: list[dict], code: str) -> list[dict]:
    jobs: list[dict] = []
    for item in items:
        if item.get("code") == code:
            jobs.extend(dict(job) for job in (item.get("context") or {}).get("jobs", []))
    return jobs


def main() -> int:
    failures = 0
    repo_root = Path(__file__).resolve().parents[1]
    audit_mod = load_project_audit(repo_root)

    with tempfile.TemporaryDirectory(prefix="qualia_project_readiness_") as tmp:
        root = Path(tmp)
        db_path = root / "scope.db"
        output_dir = root / "outputs"
        backup_dir = root / "backups"
        output_dir.mkdir()
        backup_dir.mkdir()
        create_fixture(db_path)

        before = sha256(db_path)
        global_report = audit_mod.readiness_v2.audit(db_path, output_dir, backup_dir)
        target_report = audit_mod.audit_project(db_path, output_dir, backup_dir, 1)
        other_report = audit_mod.audit_project(db_path, output_dir, backup_dir, 2)
        after = sha256(db_path)

        global_blockers = codes(global_report["blockers"])
        global_warnings = codes(global_report["warnings"])
        target_blockers = codes(target_report["blockers"])
        target_warnings = codes(target_report["warnings"])
        other_blockers = codes(other_report["blockers"])
        other_warnings = codes(other_report["warnings"])

        global_scope_jobs = issue_jobs(global_report["blockers"], "processing_job_scope_invalid")
        target_scope_jobs = issue_jobs(target_report["blockers"], "processing_job_scope_invalid")
        other_scope_jobs = issue_jobs(other_report["blockers"], "processing_job_scope_invalid")

        failures += check(
            "database-wide audit sees unrelated draft project defects",
            "mapping_flow_mismatch" in global_blockers
            and "generated_file_missing" in global_blockers
            and "unknown_speakers" in global_warnings
            and "active_processing_jobs" in global_warnings,
            f"blockers={global_blockers} warnings={global_warnings}",
        )
        failures += check(
            "database-wide audit blocks semantic processing-job scope violations",
            "processing_job_scope_invalid" in global_blockers
            and global_report.get("info", {}).get("processing_job_scope_invalid_count") == 3
            and {int(job["id"]) for job in global_scope_jobs} == {3, 4, 5},
            str(global_scope_jobs),
        )
        failures += check(
            "scope audit identifies wrong-flow and cross-project ownership reasons",
            any(
                int(job["id"]) == 3 and "question_wrong_interview_flow" in job.get("reasons", [])
                for job in global_scope_jobs
            )
            and any(
                int(job["id"]) == 4 and "interview_cross_project" in job.get("reasons", [])
                for job in global_scope_jobs
            )
            and any(
                int(job["id"]) == 5 and "question_cross_project" in job.get("reasons", [])
                for job in global_scope_jobs
            ),
            str(global_scope_jobs),
        )
        failures += check(
            "project-scoped audit excludes unrelated project content defects",
            "mapping_flow_mismatch" not in target_blockers
            and "generated_file_missing" not in target_blockers
            and "unknown_speakers" not in target_warnings
            and "active_processing_jobs" not in target_warnings,
            f"blockers={target_blockers} warnings={target_warnings}",
        )
        failures += check(
            "project-scoped audit keeps only target project's processing-job blockers",
            "processing_job_scope_invalid" in target_blockers
            and target_report.get("info", {}).get("processing_job_scope_invalid_count") == 2
            and {int(job["id"]) for job in target_scope_jobs} == {3, 4}
            and all(int(job["project_id"]) == 1 for job in target_scope_jobs),
            str(target_scope_jobs),
        )
        failures += check(
            "second project receives only its own job-scope blocker and active job warning",
            "processing_job_scope_invalid" in other_blockers
            and other_report.get("info", {}).get("processing_job_scope_invalid_count") == 1
            and {int(job["id"]) for job in other_scope_jobs} == {5}
            and "active_processing_jobs" in other_warnings
            and other_report.get("info", {}).get("active_processing_job_count") == 1,
            f"scope_jobs={other_scope_jobs} warnings={other_warnings}",
        )
        failures += check(
            "project-scoped audit reports selected scope",
            target_report.get("info", {}).get("scope") == "project"
            and target_report.get("info", {}).get("project_id") == 1
            and target_report.get("info", {}).get("active_processing_job_count") == 0,
            str(target_report.get("info", {})),
        )
        failures += check(
            "project-scoped audit leaves source SQLite bytes unchanged",
            before == after,
        )

        missing_report = audit_mod.audit_project(db_path, output_dir, backup_dir, 999)
        failures += check(
            "unknown project ID is rejected explicitly",
            "project_not_found" in codes(missing_report["blockers"]),
            str(missing_report["blockers"]),
        )

    wrapper = (repo_root / "scripts" / "check_production_readiness.ps1").read_text(encoding="utf-8")
    failures += check(
        "PowerShell readiness entrypoint routes --project-id to scoped audit",
        "--project-id" in wrapper and "audit_production_readiness_project.py" in wrapper,
    )

    if failures:
        print(f"\nSummary: FAIL ({failures} checks failed)")
        return 1
    print("\nSummary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
